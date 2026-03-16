from pathlib import Path

from app.bootstrap import build_memory, init_database, list_organizations
from app.config import Settings
from app.context import MOCK_USERS


def make_settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        openai_api_key="sk-test",
        database_url=f"sqlite:///{(tmp_path / 'nested' / 'qp.db').as_posix()}",
        chroma_path=tmp_path / "chroma",
        **overrides,
    )


def test_init_database_creates_folders_and_seeds(tmp_path: Path) -> None:
    engine = init_database(make_settings(tmp_path))
    try:
        assert [name for _, name in list_organizations(engine)] == [
            "Acme Corp",
            "Globex Industries",
            "Initech Labs",
        ]
    finally:
        engine.dispose()


def test_init_database_is_repeatable(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    init_database(settings).dispose()
    engine = init_database(settings)  # second start-up must not reseed or fail
    try:
        assert len(list_organizations(engine)) == 3
    finally:
        engine.dispose()


def test_memory_can_be_disabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, memory_enabled=False)
    assert build_memory(settings, client=None) is None  # type: ignore[arg-type]


def test_mock_users_match_seeded_organizations(tmp_path: Path) -> None:
    engine = init_database(make_settings(tmp_path))
    try:
        orgs = dict(list_organizations(engine))
        for user in MOCK_USERS.values():
            assert orgs[user.org_id] == user.org_name
    finally:
        engine.dispose()


def test_database_password_is_masked() -> None:
    settings = Settings(
        _env_file=None, database_url="postgresql+psycopg://user:s3cret@db:5432/qp"
    )
    assert "s3cret" not in settings.safe_database_url
    assert "user" in settings.safe_database_url
