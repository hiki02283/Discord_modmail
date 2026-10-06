"""Thread persistence and the rules that govern thread lifecycle.

Everything that reads or writes threads goes through :class:`ThreadManager`.
Keeping the queries here (rather than scattered through the bot) is what makes
the "one open thread per member" rule enforceable in a single place.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from modmail.db import session_scope
from modmail.models import (
    BlockedUser,
    LogEntry,
    MessageDirection,
    Thread,
    ThreadMessage,
    ThreadStatus,
)

logger = logging.getLogger(__name__)


class ThreadError(Exception):
    """Base class for thread rule violations."""


class ThreadAlreadyOpen(ThreadError):
    """The member already has an open thread."""


class ThreadNotFound(ThreadError):
    """No thread matched the lookup."""


def _with_messages(stmt):
    return stmt.options(selectinload(Thread.messages), selectinload(Thread.log_entry))


class ThreadManager:
    """Creates, finds, records messages for, and closes threads."""

    # --- Lookups ----------------------------------------------------------

    async def get_open_by_recipient(
        self, recipient_id: int, *, session: AsyncSession | None = None
    ) -> Thread | None:
        """Return the member's open thread, if any."""
        stmt = _with_messages(
            select(Thread).where(
                Thread.recipient_id == recipient_id,
                Thread.status == ThreadStatus.OPEN,
            )
        )
        if session is not None:
            return (await session.execute(stmt)).scalar_one_or_none()
        async with session_scope() as own:
            return (await own.execute(stmt)).scalar_one_or_none()

    async def get_open_by_channel(self, channel_id: int) -> Thread | None:
        """Return the open thread bound to a staff channel, if any."""
        stmt = _with_messages(
            select(Thread).where(
                Thread.channel_id == channel_id,
                Thread.status == ThreadStatus.OPEN,
            )
        )
        async with session_scope() as session:
            return (await session.execute(stmt)).scalar_one_or_none()

    async def get(self, thread_id: int) -> Thread | None:
        stmt = _with_messages(select(Thread).where(Thread.id == thread_id))
        async with session_scope() as session:
            return (await session.execute(stmt)).scalar_one_or_none()

    # --- Lifecycle --------------------------------------------------------

    async def open_thread(
        self,
        recipient_id: int,
        *,
        channel_id: int | None = None,
        recipient_name: str | None = None,
    ) -> Thread:
        """Open a thread for a member.

        Raises :class:`ThreadAlreadyOpen` if they already have one. The check
        is duplicated in the database (a partial unique index) so two
        concurrent messages from the same member cannot both succeed.
        """
        async with session_scope() as session:
            existing = await self.get_open_by_recipient(recipient_id, session=session)
            if existing is not None:
                raise ThreadAlreadyOpen(
                    f"Recipient {recipient_id} already has open thread {existing.id}."
                )

            thread = Thread(
                recipient_id=recipient_id,
                channel_id=channel_id,
                recipient_name=recipient_name,
                status=ThreadStatus.OPEN,
            )
            session.add(thread)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise ThreadAlreadyOpen(
                    f"Recipient {recipient_id} already has an open thread."
                ) from exc

            logger.info(
                "Opened thread %s for recipient %s (channel %s).",
                thread.id,
                recipient_id,
                channel_id,
            )
            return thread

    async def set_channel(self, thread_id: int, channel_id: int) -> None:
        """Bind a Discord channel to a thread once it has been created."""
        async with session_scope() as session:
            thread = await session.get(Thread, thread_id)
            if thread is None:
                raise ThreadNotFound(f"Thread {thread_id} does not exist.")
            thread.channel_id = channel_id

    async def close_thread(
        self,
        thread_id: int,
        *,
        closed_by_id: int | None = None,
    ) -> Thread:
        """Close a thread and create its archive record."""
        async with session_scope() as session:
            thread = (
                await session.execute(_with_messages(select(Thread).where(Thread.id == thread_id)))
            ).scalar_one_or_none()
            if thread is None:
                raise ThreadNotFound(f"Thread {thread_id} does not exist.")

            if thread.status is ThreadStatus.CLOSED:
                return thread

            thread.status = ThreadStatus.CLOSED
            thread.closed_at = datetime.now(UTC)
            thread.closed_by_id = closed_by_id

            if thread.log_entry is None:
                entry = LogEntry(thread_id=thread.id)
                session.add(entry)
                await session.flush()
                # Keep the relationship consistent so callers can read
                # thread.log_entry immediately after closing.
                thread.log_entry = entry

            logger.info("Closed thread %s (by %s).", thread.id, closed_by_id)
            return thread

    # --- Messages ---------------------------------------------------------

    async def record_message(
        self,
        thread_id: int,
        *,
        direction: MessageDirection,
        author_id: int,
        author_name: str | None = None,
        content: str | None = None,
        attachment_urls: list[str] | None = None,
        dm_message_id: int | None = None,
        staff_message_id: int | None = None,
    ) -> ThreadMessage:
        """Persist a relayed message so the archive is complete."""
        async with session_scope() as session:
            message = ThreadMessage(
                thread_id=thread_id,
                direction=direction,
                author_id=author_id,
                author_name=author_name,
                content=content,
                attachment_urls="\n".join(attachment_urls) if attachment_urls else None,
                dm_message_id=dm_message_id,
                staff_message_id=staff_message_id,
            )
            session.add(message)
            await session.flush()
            return message

    async def get_by_staff_message(self, staff_message_id: int) -> ThreadMessage | None:
        """Find a recorded message from its staff-channel message id (for edit sync)."""
        stmt = select(ThreadMessage).where(ThreadMessage.staff_message_id == staff_message_id)
        async with session_scope() as session:
            return (await session.execute(stmt)).scalar_one_or_none()

    # --- Archives ---------------------------------------------------------

    async def get_log_by_key(self, key: str) -> Thread | None:
        """Fetch an archived thread by its public log key."""
        stmt = _with_messages(
            select(Thread)
            .join(LogEntry, LogEntry.thread_id == Thread.id)
            .where(LogEntry.key == key)
        )
        async with session_scope() as session:
            return (await session.execute(stmt)).scalar_one_or_none()

    # --- Blocking ---------------------------------------------------------

    async def is_blocked(self, user_id: int) -> BlockedUser | None:
        """Return the block row for a user, or None when not blocked."""
        async with session_scope() as session:
            return await session.get(BlockedUser, user_id)

    async def block_user(
        self,
        user_id: int,
        *,
        reason: str | None = None,
        blocked_by_id: int | None = None,
    ) -> BlockedUser:
        """Block a user. Idempotent: re-blocking updates reason/staff/timestamp."""
        async with session_scope() as session:
            existing = await session.get(BlockedUser, user_id)
            if existing is not None:
                existing.reason = reason
                existing.blocked_by_id = blocked_by_id
                existing.updated_at = datetime.now(UTC)
                logger.info("Re-blocked user %s (by %s).", user_id, blocked_by_id)
                return existing
            row = BlockedUser(
                user_id=user_id, reason=reason, blocked_by_id=blocked_by_id
            )
            session.add(row)
            await session.flush()
            logger.info("Blocked user %s (by %s).", user_id, blocked_by_id)
            return row

    async def unblock_user(self, user_id: int) -> bool:
        """Remove a block. Returns True when a row was removed."""
        async with session_scope() as session:
            existing = await session.get(BlockedUser, user_id)
            if existing is None:
                return False
            await session.delete(existing)
            logger.info("Unblocked user %s.", user_id)
            return True

    async def list_blocked(self) -> list[BlockedUser]:
        """Return every blocked user, oldest first."""
        async with session_scope() as session:
            stmt = select(BlockedUser).order_by(BlockedUser.created_at)
            return list((await session.execute(stmt)).scalars().all())
