import pytest

from app.config import ConfigError, Settings


def test_defaults() -> None:
    settings = Settings(_env_file=None, openai_api_key=None)
    assert settings.max_tool_iterations == 5
    assert settings.is_sqlite


def test_missing_api_key_raises() -> None:
    settings = Settings(_env_file=None, openai_api_key=None)
    with pytest.raises(ConfigError):
        settings.require_openai_api_key()


def test_api_key_is_returned_but_not_in_repr() -> None:
    settings = Settings(_env_file=None, openai_api_key="sk-test-123")
    assert settings.require_openai_api_key() == "sk-test-123"
    assert "sk-test-123" not in repr(settings)
