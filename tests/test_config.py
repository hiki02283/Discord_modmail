"""Configuration loading and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modmail.config import ConfigurationError, Settings, get_settings

VALID_ENV = {
    "DISCORD_TOKEN": "123456789012345678.abcdefghijklmnop.qrstuvwxyz0123456789ABCD",
    "DISCORD_GUILD_ID": "987654321098765432",
}


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Remove any real .env values so tests are hermetic."""
    for key in (
        "DISCORD_TOKEN",
        "DISCORD_GUILD_ID",
        "MODMAIL_CATEGORY_ID",
        "LOG_CHANNEL_ID",
        "COMMAND_PREFIX",
        "DATABASE_URL",
        "LOG_LEVEL",
        "LOG_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


def test_missing_token_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, discord_guild_id=1)  # type: ignore[call-arg]


def test_blank_token_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, discord_token="   ", discord_guild_id=1)


def test_valid_settings_parse():
    settings = Settings(_env_file=None, **VALID_ENV)
    assert settings.discord_guild_id == 987654321098765432
    assert settings.command_prefix == "?"
    assert settings.is_sqlite is True


def test_guild_id_must_be_numeric():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, discord_token=VALID_ENV["DISCORD_TOKEN"], discord_guild_id="abc")


def test_invalid_log_level_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **VALID_ENV, log_level="LOUD")


def test_log_level_is_normalised():
    settings = Settings(_env_file=None, **VALID_ENV, log_level="debug")
    assert settings.log_level == "DEBUG"


def test_sqlite_path_is_absolute():
    settings = Settings(_env_file=None, **VALID_ENV, database_url="sqlite+aiosqlite:///./x.db")
    assert settings.sqlite_path is not None
    assert settings.sqlite_path.is_absolute()


def test_non_sqlite_url_has_no_path():
    settings = Settings(
        _env_file=None,
        **VALID_ENV,
        database_url="postgresql+asyncpg://user:pw@localhost/modmail",
    )
    assert settings.is_sqlite is False
    assert settings.sqlite_path is None


def test_get_settings_wraps_errors(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "")
    with pytest.raises(ConfigurationError):
        get_settings()
