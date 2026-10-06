"""Shared test fixtures.

The tests exercise the thread rules and the relay without a live Discord
connection: database work runs against an in-memory SQLite database, and
Discord objects are replaced with small stand-ins that record what they were
asked to send.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Make `src/` importable without an editable install.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from modmail import db  # noqa: E402
from modmail.thread_manager import ThreadManager  # noqa: E402


@pytest_asyncio.fixture
async def database():
    """A fresh in-memory database per test."""
    db.init_engine("sqlite+aiosqlite:///:memory:")
    await db.create_all()
    try:
        yield db
    finally:
        await db.dispose_engine()


@pytest_asyncio.fixture
async def threads(database):
    return ThreadManager()


# --- Discord stand-ins ----------------------------------------------------


class FakeAttachment:
    def __init__(self, url: str) -> None:
        self.url = url


class FakeAuthor:
    def __init__(self, user_id: int, name: str = "tester", bot: bool = False) -> None:
        self.id = user_id
        self.name = name
        self.display_name = name
        self.bot = bot
        self.mention = f"<@{user_id}>"

    def __str__(self) -> str:
        return self.name


class FakeMessage:
    """Minimal stand-in for ``discord.Message``."""

    def __init__(
        self,
        *,
        author: FakeAuthor,
        content: str = "",
        attachments: list[str] | None = None,
        message_id: int = 1,
        channel_id: int = 0,
    ) -> None:
        self.author = author
        self.content = content
        self.attachments = [FakeAttachment(url) for url in (attachments or [])]
        self.id = message_id
        self.channel = type("FakeChannel", (), {"id": channel_id})()


class FakeSendable:
    """Records messages instead of sending them to Discord."""

    def __init__(self, channel_id: int = 0, *, forbidden: bool = False) -> None:
        self.id = channel_id
        self.sent: list[dict] = []
        self._forbidden = forbidden

    async def send(self, content=None, embed=None, **kwargs):
        if self._forbidden:
            import discord

            raise discord.Forbidden(_FakeResponse(), "no permission")
        self.sent.append({"content": content, "embed": embed})
        return FakeSentMessage(message_id=1000 + len(self.sent), channel_id=self.id)

    @property
    def last_content(self) -> str | None:
        """Content of the most recent send, for assertions."""
        return self.sent[-1]["content"] if self.sent else None


class FakeSentMessage:
    def __init__(self, message_id: int, channel_id: int) -> None:
        self.id = message_id
        self.channel = type("FakeChannel", (), {"id": channel_id})()

    async def add_reaction(self, _emoji):
        return None


class _FakeResponse:
    status = 403
    reason = "Forbidden"


@pytest.fixture
def author() -> FakeAuthor:
    return FakeAuthor(111, "alice")


@pytest.fixture
def staff() -> FakeAuthor:
    return FakeAuthor(999, "moderator")
