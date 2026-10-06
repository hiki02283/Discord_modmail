"""Blocking: manager API plus the DM and staff-reply gates."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from modmail.bot import ModmailBot  # noqa: E402
from modmail.config import Settings  # noqa: E402


def _settings() -> Settings:
    return Settings(discord_token="test-token", discord_guild_id=1234)


class _DMChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)
        return None


class _DMMessage:
    def __init__(self, author_id: int, channel: _DMChannel) -> None:
        author = types.SimpleNamespace(id=author_id, name="alice")
        self.author = author
        self.channel = channel
        self.id = 1
        self.content = "hello"
        self.attachments = []


async def test_block_and_is_blocked(threads):
    assert await threads.is_blocked(111) is None
    row = await threads.block_user(111, reason="spam", blocked_by_id=999)
    assert row.user_id == 111
    assert row.reason == "spam"
    assert row.blocked_by_id == 999
    assert row.created_at is not None
    assert (await threads.is_blocked(111)).user_id == 111


async def test_duplicate_block_is_idempotent(threads):
    await threads.block_user(111, reason="first", blocked_by_id=1)
    row = await threads.block_user(111, reason="second", blocked_by_id=2)
    assert row.reason == "second"
    assert row.blocked_by_id == 2
    assert len(await threads.list_blocked()) == 1


async def test_unblock_user(threads):
    assert await threads.unblock_user(111) is False
    await threads.block_user(111)
    assert await threads.unblock_user(111) is True
    assert await threads.is_blocked(111) is None


async def test_blocklist(threads):
    assert await threads.list_blocked() == []
    await threads.block_user(111, reason="spam", blocked_by_id=999)
    await threads.block_user(222)
    rows = await threads.list_blocked()
    assert [r.user_id for r in rows] == [111, 222]
    assert rows[0].reason == "spam"


async def test_dm_blocked_gate(threads):
    bot = ModmailBot(_settings())
    bot.threads = threads
    await threads.block_user(111)
    channel = _DMChannel()
    await bot.handle_dm(_DMMessage(111, channel))
    assert channel.sent, "blocked user must get a notice"
    assert "blocked" in channel.sent[0].lower()
    assert await threads.get_open_by_recipient(111) is None


async def test_unblock_then_dm_opens_thread(threads):
    bot = ModmailBot(_settings())
    bot.threads = threads
    await threads.block_user(111)
    await threads.unblock_user(111)
    # Gate cleared: a new thread can now be opened for the user.
    assert await threads.is_blocked(111) is None
    thread = await threads.open_thread(111, channel_id=777)
    assert thread.is_open is True
    assert (await threads.get_open_by_recipient(111)).id == thread.id


async def test_staff_reply_blocked_gate(threads):
    bot = ModmailBot(_settings())
    bot.threads = threads
    thread = await threads.open_thread(111, channel_id=222)
    await threads.block_user(111)
    bot.relay.to_user = AsyncMock()
    staff_message = types.SimpleNamespace(
        channel=types.SimpleNamespace(id=222, send=AsyncMock()),
    )
    assert await bot.handle_staff_message(staff_message) is False
    bot.relay.to_user.assert_not_called()
    # Thread left intact.
    assert (await threads.get(thread.id)).status.value == "open"


async def test_staff_reply_works_after_unblock(threads):
    bot = ModmailBot(_settings())
    bot.threads = threads
    await threads.open_thread(111, channel_id=222)
    await threads.block_user(111)
    await threads.unblock_user(111)
    bot.get_user = lambda _uid: None  # noqa: ARG005
    bot.fetch_user = AsyncMock(return_value=types.SimpleNamespace(id=111))
    bot.relay.to_user = AsyncMock(return_value=types.SimpleNamespace(id=5))
    staff_message = types.SimpleNamespace(
        channel=types.SimpleNamespace(id=222),
        content="hello",
        attachments=[],
        author=types.SimpleNamespace(id=999, name="mod"),
        id=42,
        add_reaction=AsyncMock(),
    )
    assert await bot.handle_staff_message(staff_message) is True
    bot.relay.to_user.assert_awaited_once()


def test_slash_commands_registered():
    from modmail.bot import ModmailCommands

    names = {c.name for c in ModmailCommands.__cog_app_commands__}
    assert {"block", "unblock", "blocklist", "help", "reply", "close", "threadinfo"} <= names
    # Mention group children are registered on the group, not the cog directly.
    group = next(c for c in ModmailCommands.__cog_app_commands__ if c.name == "mention")
    assert {c.name for c in group.commands} >= {"on", "off", "list"}
