"""Shared start-up wiring used by the CLI now and the Gradio app later."""

from __future__ import annotations

from dataclasses import dataclass

from openai import OpenAI
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.agent.agent import Agent
from app.agent.memory import AgentMemory, openai_embedder
from app.config import Settings
from app.context import UserContext
from app.db.models import Organization, build_engine
from app.db.runner import SqlRunner
from app.db.seed import seed_database


def init_database(settings: Settings) -> Engine:
    """Create the engine, ensure data folders exist and seed demo data if the DB is empty."""
    settings.ensure_data_dirs()
    engine = build_engine(settings.database_url)
    seed_database(engine)
    return engine


def build_runner(settings: Settings, engine: Engine) -> SqlRunner:
    return SqlRunner(
        engine,
        max_rows=settings.max_result_rows,
        timeout_seconds=settings.query_timeout_seconds,
    )


def build_openai_client(settings: Settings) -> OpenAI:
    """Raises :class:`~app.config.ConfigError` if no API key is configured."""
    return OpenAI(api_key=settings.require_openai_api_key())


def build_memory(settings: Settings, client: OpenAI) -> AgentMemory | None:
    if not settings.memory_enabled:
        return None
    return AgentMemory(
        settings.chroma_path,
        openai_embedder(client, settings.embedding_model),
        max_distance=settings.memory_max_distance,
    )


def list_organizations(engine: Engine) -> list[tuple[int, str]]:
    """(id, name) of every organization; used to populate the demo identity picker."""
    with Session(engine) as session:
        rows = session.execute(select(Organization.id, Organization.name).order_by(Organization.id))
        return [(row.id, row.name) for row in rows]


@dataclass(frozen=True)
class AppContext:
    """Everything a front end needs; build once at start-up and share across sessions."""

    settings: Settings
    runner: SqlRunner
    client: OpenAI
    memory: AgentMemory | None
    organizations: list[tuple[int, str]]

    def new_agent(self, user: UserContext) -> Agent:
        """A fresh conversation for ``user`` (agents hold per-conversation history)."""
        return Agent(self.settings, self.runner, user, memory=self.memory, client=self.client)


def build_context(settings: Settings) -> AppContext:
    """Wire up database, OpenAI client and memory. Raises ConfigError without an API key."""
    engine = init_database(settings)
    client = build_openai_client(settings)
    return AppContext(
        settings=settings,
        runner=build_runner(settings, engine),
        client=client,
        memory=build_memory(settings, client),
        organizations=list_organizations(engine),
    )
