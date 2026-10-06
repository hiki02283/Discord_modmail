"""Configurable staff mentions for new member messages.

Kept separate from :mod:`modmail.thread_manager` so the mention feature can be
reasoned about (and tested) on its own, and so the relay code stays unchanged.

State lives in SQLite through two small tables: a key/value row for the on/off
toggle and one row per mentioned staff member. Only Discord IDs are stored as
the source of truth; display names are resolved live so departed members
degrade gracefully.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select

from modmail.db import session_scope
from modmail.models import GuildSetting, MentionRole, MentionTarget

logger = logging.getLogger(__name__)

ENABLED_KEY = "mentions_enabled"


class MentionSettings:
    """Reads and writes the mention configuration."""

    # --- Toggle -----------------------------------------------------------

    async def is_enabled(self) -> bool:
        """Whether staff mentions are switched on. Defaults to off."""
        async with session_scope() as session:
            setting = await session.get(GuildSetting, ENABLED_KEY)
            return setting is not None and setting.value == "true"

    async def set_enabled(self, enabled: bool) -> None:
        async with session_scope() as session:
            setting = await session.get(GuildSetting, ENABLED_KEY)
            if setting is None:
                session.add(GuildSetting(key=ENABLED_KEY, value=str(bool(enabled)).lower()))
            else:
                setting.value = str(bool(enabled)).lower()
        logger.info("Staff mentions %s.", "enabled" if enabled else "disabled")

    # --- Targets ----------------------------------------------------------

    async def list_targets(self) -> list[int]:
        """Configured staff member IDs, in the order they were added."""
        async with session_scope() as session:
            rows = (
                await session.execute(select(MentionTarget).order_by(MentionTarget.created_at))
            ).scalars()
            return [row.user_id for row in rows]

    async def add_target(self, user_id: int, *, added_by_id: int | None = None) -> bool:
        """Add a staff member. Returns False when they were already present."""
        async with session_scope() as session:
            if await session.get(MentionTarget, user_id) is not None:
                return False
            session.add(MentionTarget(user_id=user_id, added_by_id=added_by_id))
        logger.info("Added mention target %s.", user_id)
        return True

    async def remove_target(self, user_id: int) -> bool:
        """Remove a staff member. Returns False when they were not present."""
        async with session_scope() as session:
            result = await session.execute(
                delete(MentionTarget).where(MentionTarget.user_id == user_id)
            )
        if result.rowcount == 0:
            return False
        logger.info("Removed mention target %s.", user_id)
        return True

    async def clear_targets(self) -> int:
        """Remove every configured staff member. Returns how many were removed."""
        async with session_scope() as session:
            result = await session.execute(delete(MentionTarget))
        return result.rowcount or 0

    # --- Roles ------------------------------------------------------------

    async def list_roles(self) -> list[int]:
        """Configured role IDs, in the order they were added."""
        async with session_scope() as session:
            rows = (
                await session.execute(select(MentionRole).order_by(MentionRole.created_at))
            ).scalars()
            return [row.role_id for row in rows]

    async def add_role(self, role_id: int, *, added_by_id: int | None = None) -> bool:
        """Add a role. Returns False when it was already present."""
        async with session_scope() as session:
            if await session.get(MentionRole, role_id) is not None:
                return False
            session.add(MentionRole(role_id=role_id, added_by_id=added_by_id))
        logger.info("Added mention role %s.", role_id)
        return True

    async def remove_role(self, role_id: int) -> bool:
        """Remove a role. Returns False when it was not present."""
        async with session_scope() as session:
            result = await session.execute(
                delete(MentionRole).where(MentionRole.role_id == role_id)
            )
        if result.rowcount == 0:
            return False
        logger.info("Removed mention role %s.", role_id)
        return True

    async def clear_roles(self) -> int:
        """Remove every configured role. Returns how many were removed."""
        async with session_scope() as session:
            result = await session.execute(delete(MentionRole))
        return result.rowcount or 0

    # --- Rendering --------------------------------------------------------

    async def resolve_target_ids(
        self,
        *,
        author_id: int,
        resolve_role_members=None,
    ) -> list[int]:
        """Every user ID that should be pinged for a new message.

        Combines directly configured users with the members of every
        configured role, de-duplicated, and never including the member who
        opened the ticket. ``resolve_role_members`` maps a role ID to an
        iterable of member IDs; without it, roles contribute nothing (which
        keeps this usable outside a live guild).
        """
        targets = await self.list_targets()
        roles = await self.list_roles()

        seen: set[int] = set()
        resolved: list[int] = []

        def add(user_id: int) -> None:
            if user_id == author_id or user_id in seen:
                return
            seen.add(user_id)
            resolved.append(user_id)

        for user_id in targets:
            add(user_id)

        if resolve_role_members is not None:
            for role_id in roles:
                for member_id in resolve_role_members(role_id) or ():
                    add(member_id)

        return resolved

    async def mention_prefix(
        self,
        *,
        author_id: int,
        resolve_role_members=None,
        resolve_name=None,
    ) -> str:
        """The mention text to prepend to a staff-channel message.

        Returns an empty string when mentions are off, when nothing is
        configured, or when the only candidates are the person who opened the
        thread — nobody should be pinged for their own message.

        ``resolve_name`` is an optional callable mapping a user ID to a display
        name; it is only used for logging and may be omitted.
        """
        if not await self.is_enabled():
            return ""

        user_ids = await self.resolve_target_ids(
            author_id=author_id, resolve_role_members=resolve_role_members
        )
        if not user_ids:
            logger.info(
                "Staff mentions are on but nobody to ping for %s "
                "(no targets configured, or all of them are the ticket opener).",
                author_id,
            )
            return ""

        if resolve_name is not None:
            logger.debug(
                "Mentioning %s for a new message from %s.",
                ", ".join(str(resolve_name(uid)) for uid in user_ids),
                author_id,
            )

        # Explicit user mentions only: never a role ping (which would ignore
        # the ticket-opener rule) and never @everyone/@here.
        return " ".join(f"<@{user_id}>" for user_id in user_ids)
