"""Database models.

Three tables carry Phase 1:

* ``threads``        — one row per conversation between a member and staff.
* ``thread_messages`` — every relayed message, in both directions.
* ``log_entries``    — a per-thread archive record with an unguessable key,
                       ready for the log viewer that comes later.

Keeping messages in their own table (rather than a JSON blob on the thread)
means the archive can grow and be searched without rewriting thread rows.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Stable constraint names make future Alembic migrations predictable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class ThreadStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class MessageDirection(StrEnum):
    """Which way a relayed message travelled."""

    TO_STAFF = "to_staff"  # member DM -> staff channel
    TO_USER = "to_user"  # staff channel -> member DM


class Thread(Base):
    """A conversation between one member and the staff team."""

    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Discord snowflakes exceed 32-bit, so store them as 64-bit integers.
    recipient_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    status: Mapped[ThreadStatus] = mapped_column(
        Enum(ThreadStatus, native_enum=False, length=16),
        default=ThreadStatus.OPEN,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    recipient_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Lazy loading is disabled deliberately: an accidental attribute access
    # would otherwise trigger blocking I/O inside async code. Callers load
    # these explicitly with selectinload().
    messages: Mapped[list[ThreadMessage]] = relationship(
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="ThreadMessage.id",
        lazy="raise",
    )
    log_entry: Mapped[LogEntry | None] = relationship(
        back_populates="thread",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="raise",
    )

    __table_args__ = (
        # At most one open thread per member. This is the invariant the whole
        # bot depends on, so it is enforced by the database and not only by
        # application code.
        Index(
            "ix_threads_one_open_per_recipient",
            "recipient_id",
            unique=True,
            sqlite_where=(status == ThreadStatus.OPEN),
            postgresql_where=(status == ThreadStatus.OPEN),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Thread id={self.id} recipient={self.recipient_id} "
            f"channel={self.channel_id} status={self.status.value}>"
        )

    @property
    def is_open(self) -> bool:
        return self.status is ThreadStatus.OPEN


class ThreadMessage(Base):
    """A single relayed message belonging to a thread."""

    __tablename__ = "thread_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), nullable=False, index=True
    )

    direction: Mapped[MessageDirection] = mapped_column(
        Enum(MessageDirection, native_enum=False, length=16), nullable=False
    )
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    content: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Discord CDN URLs, one per line. Simple and portable; a join table can
    # replace this if attachments ever need their own metadata.
    attachment_urls: Mapped[str | None] = mapped_column(Text, nullable=True)

    # IDs of the Discord messages on both sides, so edits and deletions can be
    # mirrored later.
    dm_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    staff_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    thread: Mapped[Thread] = relationship(back_populates="messages")

    __table_args__ = (
        UniqueConstraint("thread_id", "dm_message_id", name="uq_thread_messages_dm_message"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ThreadMessage id={self.id} thread={self.thread_id} {self.direction.value}>"

    @property
    def attachments(self) -> list[str]:
        if not self.attachment_urls:
            return []
        return [url for url in self.attachment_urls.splitlines() if url.strip()]


def generate_log_key() -> str:
    """An unguessable key for a public log URL (similar to a password reset token)."""
    return secrets.token_urlsafe(24)


class LogEntry(Base):
    """Archive record for a closed thread, addressable by an unguessable key."""

    __tablename__ = "log_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    key: Mapped[str] = mapped_column(
        String(64), default=generate_log_key, nullable=False, unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    thread: Mapped[Thread] = relationship(back_populates="log_entry")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LogEntry id={self.id} thread={self.thread_id} key={self.key[:8]}...>"


class BlockedUser(Base):
    """A member blocked from creating ModMail tickets."""

    __tablename__ = "blocked_users"

    # Discord user ID is the source of truth.
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocked_by_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<BlockedUser user_id={self.user_id}>"


class GuildSetting(Base):
    """A single key/value setting for the guild.

    Small and generic on purpose: new staff preferences can be added without a
    schema change. Values are stored as text and interpreted by the caller.
    """

    __tablename__ = "guild_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<GuildSetting {self.key}={self.value!r}>"


class MentionTarget(Base):
    """A staff member who is mentioned when a new member message arrives."""

    __tablename__ = "mention_targets"

    # Discord user IDs are the source of truth; names are for display only.
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    added_by_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<MentionTarget user_id={self.user_id}>"


class MentionRole(Base):
    """A staff role whose members are mentioned when a new message arrives.

    Only the role ID is stored. The role is resolved from the guild at mention
    time, so a renamed or recoloured role keeps working.
    """

    __tablename__ = "mention_roles"

    role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    added_by_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<MentionRole role_id={self.role_id}>"
