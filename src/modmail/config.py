"""Application configuration.

All secrets come from the environment (or a local ``.env`` file). Nothing is
hard-coded, and the application refuses to start if a required value is absent.

The settings object is deliberately the *only* place that knows about
environment variable names. Everything else in the codebase receives typed
values, which keeps a future move (PostgreSQL, a web viewer) to a change of
configuration rather than a change of code.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root: <root>/src/modmail/config.py -> <root>
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Typed application settings, loaded from the environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Required ---------------------------------------------------------
    discord_token: str = Field(
        ...,
        description="Discord bot token from the Developer Portal.",
    )
    discord_guild_id: int = Field(
        ...,
        description="Guild the bot serves.",
    )

    # --- Optional ---------------------------------------------------------
    modmail_category_id: int | None = Field(
        default=None,
        description="Category holding thread channels. Falls back to a 'ModMail' category.",
    )
    log_channel_id: int | None = Field(
        default=None,
        description="Channel notified when a thread closes.",
    )
    mention_admin_role_id: int | None = Field(
        default=None,
        description=(
            "Optional role allowed to run ?mention commands, in addition to "
            "the server owner and administrators."
        ),
    )
    command_prefix: str = Field(default="?", min_length=1, max_length=5)

    database_url: str = Field(
        default="sqlite+aiosqlite:///./modmail.db",
        description="SQLAlchemy async URL. Swap for PostgreSQL without code changes.",
    )

    log_level: str = Field(default="INFO")
    log_dir: Path = Field(default=PROJECT_ROOT / "logs")

    # --- Validation -------------------------------------------------------

    @field_validator("discord_token")
    @classmethod
    def _token_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError(
                "DISCORD_TOKEN is empty. Set it in the .env file (copy env.example to .env first)."
            )
        return value

    @field_validator(
        "modmail_category_id", "log_channel_id", "mention_admin_role_id", mode="before"
    )
    @classmethod
    def _blank_id_means_unset(cls, value: object) -> object:
        """Treat an empty value in .env as "not configured" rather than an error."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}, got {value!r}.")
        return level

    @field_validator("command_prefix")
    @classmethod
    def _no_whitespace_prefix(cls, value: str) -> str:
        if value.strip() != value:
            raise ValueError("COMMAND_PREFIX must not contain surrounding whitespace.")
        return value

    # --- Derived ----------------------------------------------------------

    @property
    def is_sqlite(self) -> bool:
        """True when running on SQLite (affects a couple of engine options)."""
        return self.database_url.startswith("sqlite")

    @property
    def sqlite_path(self) -> Path | None:
        """Filesystem path of the SQLite database, if that is what we use."""
        if not self.is_sqlite:
            return None
        _, _, raw = self.database_url.partition(":///")
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_absolute() else (PROJECT_ROOT / path)


class ConfigurationError(RuntimeError):
    """Raised when configuration is missing or invalid."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, raising a readable error if invalid.

    Cached so the ``.env`` file is read once. Tests can call
    ``get_settings.cache_clear()`` to reload.
    """
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:  # pydantic ValidationError and friends
        raise ConfigurationError(
            "Invalid configuration. Copy env.example to .env and fill in the "
            f"required values.\n\n{exc}"
        ) from exc
