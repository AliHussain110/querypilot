from datetime import date

import pytest
from sqlalchemy import Engine
from sqlglot import parse_one

from app.context import MOCK_USERS, UserContext
from app.db.errors import SqlValidationError
from app.db.rls import RlsPolicy, apply_rls
from app.db.runner import SqlRunner
from app.db.seed import CUSTOMERS_PER_ORG, seed_database

ACME = MOCK_USERS["alice"]
ALL_USERS = list(MOCK_USERS.values())


@pytest.fixture
def runner(engine: Engine) -> SqlRunner:
    seed_database(engine, today=date(2026, 6, 1))
    return SqlRunner(engine)


@pytest.mark.parametrize("user", ALL_USERS, ids=lambda u: u.name)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT DISTINCT org_id FROM orders",
        "SELECT DISTINCT org_id FROM customers",
        "SELECT DISTINCT org_id FROM products",
        "SELECT DISTINCT o.org_id FROM orders o JOIN customers c ON c.id = o.customer_id",
        "SELECT org_id FROM customers UNION SELECT org_id FROM products",
        "SELECT org_id FROM orders WHERE customer_id IN (SELECT id FROM customers)",
    ],
)
def test_only_the_users_org_is_visible(runner: SqlRunner, user: UserContext, sql: str) -> None:
    assert runner.run(sql, user).rows == [[user.org_id]]


@pytest.mark.parametrize("user", ALL_USERS, ids=lambda u: u.name)
def test_users_only_see_their_own_organization_row(runner: SqlRunner, user: UserContext) -> None:
    assert runner.run("SELECT id FROM organizations", user).rows == [[user.org_id]]


def test_asking_for_another_org_returns_nothing(runner: SqlRunner) -> None:
    assert runner.run("SELECT * FROM orders WHERE org_id = 2", ACME).rows == []
    assert runner.run("SELECT * FROM organizations WHERE id = 2", ACME).rows == []


def test_outer_join_semantics_are_preserved(runner: SqlRunner) -> None:
    result = runner.run(
        "SELECT COUNT(*) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id", ACME
    )
    assert result.rows[0][0] >= CUSTOMERS_PER_ORG  # customers without orders still appear


def test_qualified_column_references_still_work(runner: SqlRunner) -> None:
    result = runner.run("SELECT orders.id FROM orders ORDER BY orders.id LIMIT 1", ACME)
    assert len(result.rows) == 1


def test_executed_sql_shows_the_rewrite(runner: SqlRunner) -> None:
    result = runner.run("SELECT COUNT(*) FROM orders", ACME)
    assert "org_id = 1" in result.sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM sqlite_master",
        "SELECT * FROM nonexistent_table",
        "WITH orders AS (SELECT * FROM orders) SELECT * FROM orders",
    ],
)
def test_unknown_tables_and_cte_shadowing_are_rejected(runner: SqlRunner, sql: str) -> None:
    with pytest.raises(SqlValidationError):
        runner.run(sql, ACME)


def test_apply_rls_rewrites_with_alias() -> None:
    tree = parse_one("SELECT * FROM orders o", read="sqlite")
    scoped = apply_rls(tree, org_id=7, policy=RlsPolicy.default(), known_tables={"orders"})
    sql = scoped.sql("sqlite")
    assert "org_id = 7" in sql
    assert "AS o" in sql
    assert tree.sql("sqlite") == "SELECT * FROM orders AS o"  # the input is not mutated


def test_default_policy_covers_all_tenant_tables() -> None:
    policy = RlsPolicy.default()
    assert policy.scope_columns["orders"] == "org_id"
    assert policy.scope_columns["customers"] == "org_id"
    assert policy.scope_columns["products"] == "org_id"
    assert policy.scope_columns["organizations"] == "id"
