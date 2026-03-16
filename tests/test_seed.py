from datetime import date

from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from app.db.models import Order, build_engine, tenant_tables
from app.db.seed import (
    CUSTOMERS_PER_ORG,
    ORDERS_PER_ORG,
    ORGANIZATIONS,
    seed_database,
)

TODAY = date(2026, 6, 1)


def test_seed_creates_expected_rows(engine: Engine) -> None:
    summary = seed_database(engine, today=TODAY)
    n_orgs = len(ORGANIZATIONS)
    assert not summary.skipped
    assert summary.organizations == n_orgs
    assert summary.customers == n_orgs * CUSTOMERS_PER_ORG
    assert summary.orders == n_orgs * ORDERS_PER_ORG
    assert summary.products == n_orgs * 10


def test_seed_is_idempotent(engine: Engine) -> None:
    first = seed_database(engine, today=TODAY)
    second = seed_database(engine, today=TODAY)
    assert second.skipped
    assert (second.customers, second.orders) == (first.customers, first.orders)


def test_reset_reseeds_deterministically(engine: Engine) -> None:
    seed_database(engine, today=TODAY)
    total_before = _revenue(engine)
    summary = seed_database(engine, reset=True, today=TODAY)
    assert not summary.skipped
    assert _revenue(engine) == total_before


def test_tenant_data_never_crosses_organizations(engine: Engine) -> None:
    seed_database(engine, today=TODAY)
    with engine.connect() as conn:
        mismatches = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM orders o
                JOIN customers c ON c.id = o.customer_id
                JOIN products p ON p.id = o.product_id
                WHERE o.org_id <> c.org_id OR o.org_id <> p.org_id
                """
            )
        ).scalar_one()
    assert mismatches == 0


def test_orders_are_within_the_last_year(engine: Engine) -> None:
    seed_database(engine, today=TODAY)
    with Session(engine) as session:
        latest = session.scalar(select(func.max(Order.ordered_at)))
        earliest = session.scalar(select(func.min(Order.ordered_at)))
    assert latest is not None and earliest is not None
    assert latest <= TODAY
    assert (TODAY - earliest).days <= 365


def test_tenant_tables_are_discovered_from_metadata() -> None:
    assert tenant_tables() == {"customers", "products", "orders"}


def test_foreign_keys_are_enforced_on_sqlite(engine: Engine) -> None:
    seed_database(engine, today=TODAY)
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def _revenue(engine: Engine) -> float:
    with Session(engine) as session:
        return session.scalar(select(func.sum(Order.total_amount))) or 0.0


def test_build_engine_accepts_in_memory_sqlite() -> None:
    eng = build_engine("sqlite://")
    try:
        assert seed_database(eng, today=TODAY).orders > 0
    finally:
        eng.dispose()
