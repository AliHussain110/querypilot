"""SQLAlchemy models for the demo multi-tenant commerce database.

Every table that holds tenant data carries an ``org_id`` column. The row-level
security layer discovers those tables from the metadata via :func:`tenant_tables`,
so adding a new tenant table needs no extra RLS configuration.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import (
    CheckConstraint,
    Date,
    Engine,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ORDER_STATUSES: tuple[str, ...] = ("completed", "pending", "cancelled", "refunded")
TENANT_COLUMN = "org_id"
TENANT_ROOT_TABLE = "organizations"  # its ``id`` is the org id


class Base(DeclarativeBase):
    pass


class Organization(Base):
    """A tenant. Global table: it has no ``org_id`` of its own (its ``id`` *is* the org id)."""

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    plan: Mapped[str] = mapped_column(String(20))  # free | pro | enterprise
    created_at: Mapped[date] = mapped_column(Date)


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("org_id", "email", name="uq_customers_org_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(150))
    country: Mapped[str] = mapped_column(String(60))
    signup_date: Mapped[date] = mapped_column(Date)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(60))
    price: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False))


class Order(Base):
    """One order line: a customer buying a quantity of a single product."""

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in ORDER_STATUSES) + ")",
            name="ck_orders_status",
        ),
        Index("ix_orders_org_ordered_at", "org_id", "ordered_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    quantity: Mapped[int]
    total_amount: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False))
    status: Mapped[str] = mapped_column(String(20))
    ordered_at: Mapped[date] = mapped_column(Date)


def tenant_tables() -> frozenset[str]:
    """Names of all tables that carry an ``org_id`` column (i.e. need RLS)."""
    return frozenset(t.name for t in Base.metadata.sorted_tables if TENANT_COLUMN in t.c)


def build_engine(url: str, *, echo: bool = False) -> Engine:
    """Create an engine for SQLite or Postgres with sensible defaults."""
    if url.startswith("sqlite"):
        # Gradio serves requests from worker threads, so allow cross-thread use.
        engine = create_engine(url, echo=echo, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine
    return create_engine(url, echo=echo, pool_pre_ping=True)


def init_db(engine: Engine) -> None:
    """Create all tables if they do not exist yet."""
    Base.metadata.create_all(engine)
