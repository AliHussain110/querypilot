"""Application settings, loaded from environment variables and an optional ``.env`` file."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class Settings(BaseSettings):
    """Typed, immutable configuration. Field ``foo_bar`` is read from env var ``FOO_BAR``."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # LLM
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o-mini"

    # Database
    database_url: str = f"sqlite:///{(DATA_DIR / 'querypilot.db').as_posix()}"
    max_result_rows: int = Field(200, ge=1, le=10_000)
    query_timeout_seconds: int = Field(15, ge=1, le=300)

    # Agent
    max_tool_iterations: int = Field(5, ge=1, le=10)
    memory_enabled: bool = True
    memory_top_k: int = Field(5, ge=1, le=10)
    memory_max_distance: float = Field(0.55, ge=0.0, le=2.0)  # cosine distance cut-off
    embedding_model: str = "text-embedding-3-small"
    chroma_path: Path = DATA_DIR / "chroma"

    # Web UI
    server_host: str = "127.0.0.1"  # use 0.0.0.0 inside Docker
    server_port: int = Field(7860, ge=1, le=65535)

    # Misc
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @property
    def is_sqlite(self) -> bool:
        return make_url(self.database_url).get_backend_name() == "sqlite"

    @property
    def safe_database_url(self) -> str:
        """The database URL with any password masked, safe to print or log."""
        return make_url(self.database_url).render_as_string(hide_password=True)

    def require_openai_api_key(self) -> str:
        """Return the API key or raise a helpful error."""
        if self.openai_api_key is None or not self.openai_api_key.get_secret_value():
            raise ConfigError(
                "OPENAI_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        return self.openai_api_key.get_secret_value()

    def ensure_data_dirs(self) -> None:
        """Create the folders that hold the SQLite file and Chroma persistence."""
        self.chroma_path.mkdir(parents=True, exist_ok=True)
        url = make_url(self.database_url)
        if self.is_sqlite and url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def configure_logging(level: str | None = None) -> None:
    """Set up root logging once, using the configured level by default."""
    logging.basicConfig(
        level=level or get_settings().log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
