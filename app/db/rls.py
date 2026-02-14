"""Row-level security by query rewriting.

Every reference to a protected table is replaced by a filtered subquery, keeping the
original alias::

    SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id
    -- becomes
    SELECT * FROM (SELECT * FROM orders WHERE org_id = 7) AS o
      JOIN (SELECT * FROM customers WHERE org_id = 7) AS c ON c.id = o.customer_id

Filtering the *table* (rather than appending to a WHERE clause) is correct for outer joins,
CTEs, subqueries and UNIONs alike, because no reference can escape the filter.
Tables outside the known schema (``sqlite_master``, ``pg_catalog`` ...) are rejected.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from sqlglot import exp

from app.db.errors import SqlValidationError
from app.db.models import TENANT_COLUMN, TENANT_ROOT_TABLE, tenant_tables


class RlsError(SqlValidationError):
    """The query can't be scoped to the user's organization."""


@dataclass(frozen=True)
class RlsPolicy:
    """Maps each protected table to the column that must equal the user's ``org_id``."""

    scope_columns: Mapping[str, str]

    @classmethod
    def default(cls) -> RlsPolicy:
        columns = {table: TENANT_COLUMN for table in tenant_tables()}
        columns[TENANT_ROOT_TABLE] = "id"  # organizations: a user may only see their own row
        return cls(columns)


def apply_rls(
    statement: exp.Expression,
    *,
    org_id: int,
    policy: RlsPolicy,
    known_tables: Collection[str],
) -> exp.Expression:
    """Return a copy of ``statement`` restricted to ``org_id``'s rows."""
    tree = statement.copy()
    known = {name.lower() for name in known_tables}
    cte_names = {cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias}

    for table in list(tree.find_all(exp.Table)):  # snapshot: we replace nodes while walking
        name = table.name.lower()
        if not name:
            continue
        if name in cte_names:
            if name in policy.scope_columns:
                raise RlsError(
                    f"The CTE name {table.name!r} clashes with a protected table; rename the CTE."
                )
            continue
        if name not in known:
            raise RlsError(f"Table {table.name!r} does not exist or is not accessible.")
        column = policy.scope_columns.get(name)
        if column is not None:
            table.replace(_scoped_subquery(table, column, org_id))
    return tree


def _scoped_subquery(table: exp.Table, column: str, org_id: int) -> exp.Subquery:
    inner = (
        exp.select("*")
        .from_(exp.Table(this=exp.to_identifier(table.name)))
        .where(exp.column(column).eq(exp.Literal.number(int(org_id))))
    )
    alias = table.args.get("alias")
    alias = alias.copy() if alias else exp.TableAlias(this=exp.to_identifier(table.name))
    return exp.Subquery(this=inner, alias=alias)
