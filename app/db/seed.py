"""Seed the demo database: three organisations with distinct catalogs and order history.

Usage::

    python -m app.db.seed            # create + seed if empty (idempotent)
    python -m app.db.seed --reset    # drop everything and reseed

Data is generated from a fixed random seed, so every run produces the same demo data
(dates are relative to ``today`` so "last month" style questions stay meaningful).
"""

from __future__ import annotations

import argparse
import logging
import random
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.config import configure_logging, get_settings
from app.db.models import (
    ORDER_STATUSES,
    Base,
    Customer,
    Order,
    Organization,
    Product,
    build_engine,
    init_db,
)

logger = logging.getLogger(__name__)

DEFAULT_SEED = 42
CUSTOMERS_PER_ORG = 30
ORDERS_PER_ORG = 180
STATUS_WEIGHTS = (75, 10, 8, 7)  # aligned with ORDER_STATUSES

ORGANIZATIONS: list[tuple[int, str, str]] = [
    (1, "Acme Corp", "enterprise"),  # consumer electronics
    (2, "Globex Industries", "pro"),  # outdoor gear
    (3, "Initech Labs", "free"),  # B2B software & services
]

# (name, category, price) per org id
CATALOGS: dict[int, list[tuple[str, str, float]]] = {
    1: [
        ("Wireless Headphones", "Audio", 129.99),
        ("Bluetooth Speaker", "Audio", 59.90),
        ('4K Monitor 27"', "Displays", 329.00),
        ("Mechanical Keyboard", "Accessories", 89.50),
        ("Ergonomic Mouse", "Accessories", 49.99),
        ("USB-C Docking Station", "Accessories", 159.00),
        ("Laptop Stand", "Accessories", 34.95),
        ("HD Webcam", "Video", 74.00),
        ("Noise-Cancelling Earbuds", "Audio", 199.00),
        ("Smart LED Desk Lamp", "Home Office", 42.50),
    ],
    2: [
        ("Trail Running Shoes", "Footwear", 119.00),
        ("Waterproof Jacket", "Apparel", 179.99),
        ("2-Person Tent", "Camping", 249.00),
        ("Sleeping Bag -10C", "Camping", 139.50),
        ("Hiking Backpack 50L", "Packs", 164.00),
        ("Trekking Poles", "Accessories", 64.90),
        ("Camping Stove", "Camping", 54.00),
        ("Insulated Water Bottle", "Accessories", 27.95),
        ("Headlamp 400 Lumens", "Accessories", 39.00),
        ("Merino Wool Base Layer", "Apparel", 84.00),
    ],
    3: [
        ("Team Plan - Monthly", "Subscriptions", 49.00),
        ("Team Plan - Annual", "Subscriptions", 490.00),
        ("Enterprise Seat Pack (10)", "Subscriptions", 1200.00),
        ("Onboarding Workshop", "Services", 750.00),
        ("Priority Support Add-on", "Add-ons", 199.00),
        ("API Credits 100k", "Add-ons", 89.00),
        ("Custom Integration", "Services", 2400.00),
        ("Analytics Module", "Add-ons", 320.00),
        ("Security Audit", "Services", 1800.00),
        ("Training Course Pass", "Services", 149.00),
    ],
}

COUNTRIES: dict[int, list[str]] = {
    1: ["United States", "Canada", "United Kingdom", "Germany"],
    2: ["Germany", "Switzerland", "Norway", "United States", "Canada"],
    3: ["United States", "United Kingdom", "India", "Singapore", "Australia"],
}

FIRST_NAMES = [
    "Emma", "Liam", "Olivia", "Noah", "Ava", "Lucas", "Mia", "Ethan", "Sofia", "Mason",
    "Isabella", "Logan", "Amelia", "James", "Harper", "Elijah", "Layla", "Aarav", "Chloe",
    "Omar", "Zoe", "Mateo", "Nora", "Hiro",
]  # fmt: skip
LAST_NAMES = [
    "Smith", "Johnson", "Brown", "Garcia", "Miller", "Davis", "Martinez", "Anderson", "Taylor",
    "Thomas", "Moore", "Jackson", "Martin", "Lee", "Khan", "Patel", "Nguyen", "Kim", "Silva",
    "Rossi", "Muller", "Novak", "Haddad", "Tanaka",
]  # fmt: skip


@dataclass(frozen=True)
class SeedSummary:
    organizations: int
    customers: int
    products: int
    orders: int
    skipped: bool  # True when the database already contained data


def seed_database(
    engine: Engine,
    *,
    reset: bool = False,
    today: date | None = None,
    rng_seed: int = DEFAULT_SEED,
) -> SeedSummary:
    """Create tables and populate demo data. Idempotent unless ``reset`` is set."""
    today = today or date.today()
    if reset:
        logger.info("Dropping all tables")
        Base.metadata.drop_all(engine)
    init_db(engine)

    with Session(engine) as session:
        if session.scalar(select(func.count()).select_from(Organization)):
            logger.info("Database already seeded; skipping")
            return _summarize(session, skipped=True)

        rng = random.Random(rng_seed)
        for org_id, name, plan in ORGANIZATIONS:
            session.add(
                Organization(
                    id=org_id,
                    name=name,
                    plan=plan,
                    created_at=today - timedelta(days=rng.randint(800, 1500)),
                )
            )
        session.flush()

        for org_id, _, _ in ORGANIZATIONS:
            _seed_org(session, org_id, rng, today)

        session.commit()
        summary = _summarize(session, skipped=False)

    logger.info("Seeded %s", summary)
    return summary


def _seed_org(session: Session, org_id: int, rng: random.Random, today: date) -> None:
    customers = [
        Customer(
            org_id=org_id,
            name=f"{first} {last}",
            email=f"{first}.{last}{i}@example.com".lower(),
            country=rng.choice(COUNTRIES[org_id]),
            signup_date=today - timedelta(days=rng.randint(30, 700)),
        )
        for i in range(CUSTOMERS_PER_ORG)
        for first, last in [(rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES))]
    ]
    products = [
        Product(org_id=org_id, name=name, category=category, price=price)
        for name, category, price in CATALOGS[org_id]
    ]
    session.add_all([*customers, *products])
    session.flush()  # assign primary keys

    # Skewed weights so a few customers order a lot ("top customers" is a meaningful question).
    customer_weights = [1 / (i + 1) ** 0.8 for i in range(len(customers))]
    one_year_ago = today - timedelta(days=365)

    orders: list[Order] = []
    for _ in range(ORDERS_PER_ORG):
        customer = rng.choices(customers, weights=customer_weights)[0]
        product = rng.choice(products)
        quantity = rng.choices([1, 2, 3, 4, 5], weights=[55, 25, 10, 6, 4])[0]
        start = max(customer.signup_date, one_year_ago)
        orders.append(
            Order(
                org_id=org_id,
                customer_id=customer.id,
                product_id=product.id,
                quantity=quantity,
                total_amount=round(product.price * quantity, 2),
                status=rng.choices(ORDER_STATUSES, weights=STATUS_WEIGHTS)[0],
                ordered_at=start + timedelta(days=rng.randint(0, (today - start).days)),
            )
        )
    session.add_all(orders)


def _summarize(session: Session, *, skipped: bool) -> SeedSummary:
    def count(model: type[Base]) -> int:
        return session.scalar(select(func.count()).select_from(model)) or 0

    return SeedSummary(
        organizations=count(Organization),
        customers=count(Customer),
        products=count(Product),
        orders=count(Order),
        skipped=skipped,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the QueryPilot demo database.")
    parser.add_argument("--reset", action="store_true", help="drop all tables and reseed")
    parser.add_argument("--database-url", help="override DATABASE_URL")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    settings.ensure_data_dirs()

    engine = build_engine(args.database_url or settings.database_url)
    summary = seed_database(engine, reset=args.reset)
    state = "already seeded" if summary.skipped else "seeded"
    print(
        f"Database {state}: {summary.organizations} organizations, {summary.customers} customers, "
        f"{summary.products} products, {summary.orders} orders."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
