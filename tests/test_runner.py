from datetime import date

import pytest
from sqlalchemy import Engine, text

from app.context import MOCK_USERS
from app.db.errors import SqlExecutionError, SqlValidationError
from app.db.runner import SqlRunner, describe_schema
from app.db.seed import seed_database

ACME = MOCK_USERS["alice"]


@pytest.fixture
def runner(engine: Engine) -> SqlRunner:
    seed_database(engine, today=date(2026, 6, 1))
    return SqlRunner(engine, max_rows=5, timeout_seconds=5)


def test_select_returns_columns_and_rows(runner: SqlRunner) -> None:
    result = runner.run("SELECT id, name FROM organizations ORDER BY id;", ACME)
    assert result.columns == ["id", "name"]
    assert result.rows == [[1, "Acme Corp"]]  # RLS: Alice only sees her own organization
    assert not result.truncated


def test_cte_is_allowed(runner: SqlRunner) -> None:
    result = runner.run("WITH o AS (SELECT id FROM orders) SELECT COUNT(*) AS n FROM o", ACME)
    assert result.rows == [[180]]


def test_row_cap_truncates(runner: SqlRunner) -> None:
    result = runner.run("SELECT id FROM orders", ACME)
    assert result.row_count == 5
    assert result.truncated


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "DELETE FROM orders",
        "DROP TABLE orders",
        "UPDATE orders SET status = 'completed'",
        "INSERT INTO organizations VALUES (9, 'X', 'free', '2026-01-01')",
        "SELECT 1; SELECT 2",
        "PRAGMA table_info(orders)",
        "SELECT load_extension('evil')",
    ],
)
def test_unsafe_sql_is_rejected(runner: SqlRunner, sql: str) -> None:
    with pytest.raises(SqlValidationError):
        runner.run(sql, ACME)


def test_rejected_write_leaves_data_untouched(runner: SqlRunner, engine: Engine) -> None:
    with pytest.raises(SqlValidationError):
        runner.run("DELETE FROM organizations", ACME)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM organizations")).scalar_one() == 3


def test_database_errors_become_execution_errors(runner: SqlRunner) -> None:
    with pytest.raises(SqlExecutionError, match="nope"):
        runner.run("SELECT nope FROM orders", ACME)


def test_connection_is_writable_again_after_a_query(runner: SqlRunner, engine: Engine) -> None:
    runner.run("SELECT 1", ACME)
    with engine.begin() as conn:  # would fail if query_only leaked back into the pool
        conn.execute(text("UPDATE organizations SET plan = 'pro' WHERE id = 3"))


def test_colons_and_percent_in_literals_are_preserved(runner: SqlRunner) -> None:
    result = runner.run("SELECT 'a :x' AS v, '100%' AS p", ACME)
    assert result.rows == [["a :x", "100%"]]


def test_runaway_query_hits_the_timeout(engine: Engine) -> None:
    seed_database(engine, today=date(2026, 6, 1))
    slow = SqlRunner(engine, timeout_seconds=1)
    with pytest.raises(SqlExecutionError, match="time limit"):
        slow.run(
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) "
            "SELECT COUNT(*) FROM r",
            ACME,
        )


def test_describe_schema_lists_tables_and_foreign_keys(engine: Engine) -> None:
    seed_database(engine, today=date(2026, 6, 1))
    schema = describe_schema(engine)
    for table in ("organizations", "customers", "products", "orders"):
        assert table in schema
    assert "org_id INTEGER" in schema
    assert "-> organizations.id" in schema
