"""Relaying messages between a member's DM and their staff channel.

This module holds the translation logic only: it decides *what* to send and
*where*, and records every relayed message through the thread manager. It does
not create channels or handle commands — that is the bot layer's job — which
keeps it straightforward to test without a live Discord connection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import discord

from modmail.models import MessageDirection
from modmail.thread_manager import ThreadManager

logger = logging.getLogger(__name__)

# What a ModMail member sees as the sender of every staff reply. Staff
# identities stay internal to the staff channel and the archive.
STAFF_DISPLAY_NAME = "ModMail Staff"


@dataclass
class RelayPayload:
    """A Discord message flattened into what the relay needs."""

    content: str = ""
    attachments: list[str] = field(default_factory=list)
    author_id: int = 0
    author_name: str = ""
    message_id: int | None = None

    @classmethod
    def from_message(cls, message: discord.Message) -> RelayPayload:
        return cls(
            content=message.content or "",
            attachments=[attachment.url for attachment in message.attachments],
            author_id=message.author.id,
            author_name=str(message.author),
            message_id=message.id,
        )

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to send (e.g. a sticker-only message)."""
        return not self.content.strip() and not self.attachments

    @property
    def truncated(self) -> str:
        """Content clipped to Discord's limit, leaving room for an embed."""
        limit = 2000
        if len(self.content) <= limit:
            return self.content
        return self.content[: limit - 3] + "..."


def build_staff_embed(payload: RelayPayload, *, recipient_name: str) -> discord.Embed:
    """The embed posted into the staff channel for a member's message."""
    embed = discord.Embed(
        description=payload.truncated or None,
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=f"{recipient_name} (member)")
    if payload.attachments:
        embed.add_field(
            name="Attachments",
            value="\n".join(payload.attachments[:5]),
            inline=False,
        )
    return embed


def build_user_embed(payload: RelayPayload) -> discord.Embed:
    """The embed DM'd to the member for a staff message.

    The author is always a generic label: a member must never learn which
    staff account answered them.
    """
    embed = discord.Embed(
        description=payload.truncated or None,
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=STAFF_DISPLAY_NAME)
    if payload.attachments:
        embed.add_field(
            name="Attachments",
            value="\n".join(payload.attachments[:5]),
            inline=False,
        )
    return embed


class Relay:
    """Moves messages between DMs and staff channels, and records them."""

    def __init__(self, threads: ThreadManager) -> None:
        self.threads = threads

    async def to_staff(
        self,
        *,
        thread,
        channel: discord.abc.Messageable,
        payload: RelayPayload,
        mention_prefix: str = "",
    ) -> discord.Message | None:
        """Send a member's message into the staff channel.

        ``mention_prefix`` is optional staff mention text prepended to the
        message. Empty by default, which keeps the output identical to before.
        """
        if payload.is_empty:
            return None

        embed = build_staff_embed(payload, recipient_name=thread.recipient_name or "Member")
        content = mention_prefix.strip() or None
        try:
            sent = await channel.send(content=content, embed=embed)
        except discord.Forbidden:
            logger.warning("Cannot post to staff channel %s for thread %s.", channel.id, thread.id)
            return None
        except discord.HTTPException:
            logger.exception("Failed to relay message to staff for thread %s.", thread.id)
            return None

        await self.threads.record_message(
            thread.id,
            direction=MessageDirection.TO_STAFF,
            author_id=payload.author_id,
            author_name=payload.author_name,
            content=payload.content,
            attachment_urls=payload.attachments,
            dm_message_id=payload.message_id,
            staff_message_id=sent.id,
        )
        return sent

    async def to_user(
        self,
        *,
        thread,
        recipient: discord.abc.Messageable,
        payload: RelayPayload,
    ) -> discord.Message | None:
        """Send a staff message back to the member as a DM.

        The embed always carries the generic staff label; the real staff
        identity stays in the archive record only.
        """
        if payload.is_empty:
            return None

        embed = build_user_embed(payload)
        try:
            sent = await recipient.send(embed=embed)
        except discord.Forbidden:
            logger.info(
                "Cannot DM recipient %s (thread %s): DMs closed.", thread.recipient_id, thread.id
            )
            return None
        except discord.HTTPException:
            logger.exception("Failed to relay message to user for thread %s.", thread.id)
            return None

        await self.threads.record_message(
            thread.id,
            direction=MessageDirection.TO_USER,
            author_id=payload.author_id,
            author_name=payload.author_name,
            content=payload.content,
            attachment_urls=payload.attachments,
            staff_message_id=payload.message_id,
        )
        return sent
