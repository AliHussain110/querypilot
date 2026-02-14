"""Safe, read-only SQL execution.

Defence in depth, so that a model hallucination or prompt injection can't modify data:

1. **Static validation** (sqlglot): exactly one statement, it must be a query (SELECT /
   set operation / CTE), no DML/DDL nodes anywhere in the tree, no dangerous functions.
2. **Row-level security**: every protected table is replaced by an ``org_id``-filtered subquery
   (see :mod:`app.db.rls`) before execution.
3. **Read-only session**: SQLite ``PRAGMA query_only`` / Postgres ``SET TRANSACTION READ ONLY``.
4. **Limits**: wall-clock timeout and a hard row cap.

The statement that is executed is the *re-generated* SQL from the validated syntax tree,
never the raw model output. (For production Postgres, also connect with a read-only role.)
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from datetime import time as dt_time
from decimal import Decimal
from functools import cached_property
from typing import Any

import sqlglot
from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.context import UserContext
from app.db.errors import SqlExecutionError, SqlValidationError
from app.db.rls import RlsPolicy, apply_rls

logger = logging.getLogger(__name__)

_DIALECTS = {"sqlite": "sqlite", "postgresql": "postgres"}
_MAX_SQL_CHARS = 10_000

_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name)
    for name in (
        "Insert", "Update", "Delete", "Merge", "Into", "Create", "Drop", "Alter", "Command"
    )
    if hasattr(exp, name)
)
_DENIED_FUNCTIONS = frozenset(
    {
        "load_extension", "readfile", "writefile", "randomblob", "zeroblob",  # sqlite
        "pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "lo_import",  # postgres
        "lo_export", "dblink", "dblink_exec", "set_config", "pg_terminate_backend",
        "pg_cancel_backend",
    }
)  # fmt: skip

# Mirrors SQLAlchemy's bind-parameter regex so literals like 'a :x' aren't treated as binds.
_BIND_PATTERN = re.compile(r"(?<![:\w\\]):(\w+)(?!:)")


@dataclass(frozen=True)
class QueryResult:
    sql: str  # the SQL that was actually executed
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool  # True when more rows existed than the row cap allowed
    elapsed_ms: float

    @property
    def row_count(self) -> int:
        return len(self.rows)


class SqlRunner:
    """Validates and executes read-only SQL against the configured database."""

    def __init__(
        self,
        engine: Engine,
        *,
        max_rows: int = 200,
        timeout_seconds: int = 15,
        policy: RlsPolicy | None = None,
    ) -> None:
        dialect = _DIALECTS.get(engine.dialect.name)
        if dialect is None:
            raise ValueError(f"Unsupported database dialect: {engine.dialect.name!r}")
        self._engine = engine
        self._dialect = dialect
        self._max_rows = max_rows
        self._timeout = timeout_seconds
        self._policy = policy or RlsPolicy.default()

    @cached_property
    def _known_tables(self) -> frozenset[str]:
        return frozenset(inspect(self._engine).get_table_names())

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def dialect(self) -> str:
        """sqlglot dialect name: ``sqlite`` or ``postgres``."""
        return self._dialect

    def validate(self, sql: str) -> exp.Expression:
        """Parse ``sql`` and return its syntax tree, or raise :class:`SqlValidationError`."""
        if not sql or not sql.strip():
            raise SqlValidationError("The SQL is empty.")
        if len(sql) > _MAX_SQL_CHARS:
            raise SqlValidationError(f"The SQL is too long (limit {_MAX_SQL_CHARS} characters).")
        try:
            statements = [s for s in sqlglot.parse(sql, read=self._dialect) if s is not None]
        except SqlglotError as exc:
            raise SqlValidationError(f"Could not parse the SQL: {exc}") from exc

        if len(statements) != 1:
            raise SqlValidationError("Exactly one SQL statement is allowed.")
        statement = statements[0]
        if not isinstance(statement, exp.Query):
            raise SqlValidationError("Only read-only SELECT queries are allowed.")
        if statement.find(*_FORBIDDEN_NODES) is not None:
            raise SqlValidationError("Only read-only SELECT queries are allowed.")
        for func in statement.find_all(exp.Func):
            name = func.name if isinstance(func, exp.Anonymous) else func.sql_name()
            if name.lower() in _DENIED_FUNCTIONS:
                raise SqlValidationError(f"Function {name!r} is not allowed.")
        return statement

    def run(self, sql: str, user: UserContext) -> QueryResult:
        """Validate, scope to ``user``'s organization and execute ``sql``.

        Raises :class:`~app.db.errors.SqlError` subclasses on failure.
        """
        scoped = apply_rls(
            self.validate(sql),
            org_id=user.org_id,
            policy=self._policy,
            known_tables=self._known_tables,
        )
        executable = scoped.sql(dialect=self._dialect)
        logger.debug("Executing SQL: %s", executable)

        started = time.perf_counter()
        try:
            with self._engine.connect() as conn:
                restore = self._enter_read_only(conn)
                try:
                    result = conn.execute(text(_escape_binds(executable)))
                    columns = list(result.keys())
                    fetched = result.fetchmany(self._max_rows + 1)
                finally:
                    restore()
        except SQLAlchemyError as exc:
            raise self._to_execution_error(exc) from exc

        truncated = len(fetched) > self._max_rows
        rows = [[_jsonable(v) for v in row] for row in fetched[: self._max_rows]]
        return QueryResult(
            sql=executable,
            columns=columns,
            rows=rows,
            truncated=truncated,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def _enter_read_only(self, conn: Connection) -> Callable[[], None]:
        """Put the session in read-only mode with a timeout; return a function that undoes it."""
        if self._dialect == "sqlite":
            raw = conn.connection.driver_connection
            conn.exec_driver_sql("PRAGMA query_only = ON")
            deadline = time.monotonic() + self._timeout
            raw.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)

            def restore() -> None:  # the connection goes back to the pool, so undo our changes
                raw.set_progress_handler(None, 0)
                conn.exec_driver_sql("PRAGMA query_only = OFF")

            return restore

        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(self._timeout * 1000)}")
        return lambda: None

    def _to_execution_error(self, exc: SQLAlchemyError) -> SqlExecutionError:
        message = str(getattr(exc, "orig", None) or exc).strip().splitlines()[0]
        lowered = message.lower()
        if "interrupted" in lowered or "statement timeout" in lowered:
            return SqlExecutionError(
                f"The query exceeded the {self._timeout}s time limit. Simplify it or add filters."
            )
        return SqlExecutionError(message or "Unknown database error.")


def describe_schema(engine: Engine) -> str:
    """Render the database schema as compact text for the LLM prompt."""
    inspector = inspect(engine)
    blocks: list[str] = []
    for table in sorted(inspector.get_table_names()):
        pk = set(inspector.get_pk_constraint(table).get("constrained_columns") or [])
        fks = {
            fk["constrained_columns"][0]: f"{fk['referred_table']}.{fk['referred_columns'][0]}"
            for fk in inspector.get_foreign_keys(table)
        }
        lines = [table]
        for col in inspector.get_columns(table):
            line = f"  {col['name']} {col['type']}"
            if col["name"] in pk:
                line += " PRIMARY KEY"
            if col["name"] in fks:
                line += f" -> {fks[col['name']]}"
            lines.append(line)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _escape_binds(sql: str) -> str:
    return _BIND_PATTERN.sub(r"\\:\1", sql)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date | dt_time):
        return value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(value)} bytes>"
    return value
