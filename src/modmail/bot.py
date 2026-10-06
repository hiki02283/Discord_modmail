"""The Discord bot: gateway wiring, channel creation, and commands.

Deliberately thin. Anything that can be decided without Discord lives in
``thread_manager.py`` or ``relay.py``; this module translates events into calls
on those.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

from modmail.config import Settings
from modmail.mentions import MentionSettings
from modmail.relay import Relay, RelayPayload
from modmail.thread_manager import ThreadAlreadyOpen, ThreadManager

logger = logging.getLogger(__name__)

CATEGORY_NAME = "ModMail"
MAX_CHANNEL_NAME = 90

# Added to a member's DM once it has been relayed to the staff channel.
DELIVERED_EMOJI = "\N{WHITE HEAVY CHECK MARK}"


class ModmailBot(commands.Bot):
    """A bot that turns member DMs into private staff channels."""

    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.dm_messages = True

        # No prefix commands: the whole interface is slash commands.
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )
        self.settings = settings
        self.threads = ThreadManager()
        self.relay = Relay(self.threads)
        self.mentions = MentionSettings()

        # One queue per member so a burst of DMs cannot be relayed out of
        # order. Discord does not guarantee delivery order across events.
        self._queues: dict[int, asyncio.Queue] = defaultdict(asyncio.Queue)
        self._workers: dict[int, asyncio.Task] = {}

    # --- Setup ------------------------------------------------------------

    async def setup_hook(self) -> None:
        await self.add_cog(ModmailCommands(self))
        # Sync to the configured guild so commands appear immediately instead
        # of waiting on the global command cache.
        guild = discord.Object(id=self.settings.discord_guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        logger.info("Synced %d slash command(s) to guild %s.", len(synced), guild.id)

    async def on_ready(self) -> None:
        logger.info("Connected. Serving guild %s.", self.settings.discord_guild_id)

    @property
    def guild(self) -> discord.Guild | None:
        return self.get_guild(self.settings.discord_guild_id)

    async def get_category(self) -> discord.CategoryChannel | None:
        """Find or create the category that holds thread channels."""
        guild = self.guild
        if guild is None:
            logger.error("Guild %s not found; is the bot invited?", self.settings.discord_guild_id)
            return None

        if self.settings.modmail_category_id:
            category = guild.get_channel(self.settings.modmail_category_id)
            if isinstance(category, discord.CategoryChannel):
                return category
            logger.warning("MODMAIL_CATEGORY_ID is not a category; falling back.")

        existing = discord.utils.get(guild.categories, name=CATEGORY_NAME)
        if existing is not None:
            return existing

        try:
            return await guild.create_category(CATEGORY_NAME)
        except discord.Forbidden:
            logger.error("Missing 'Manage Channels' permission to create the ModMail category.")
            return None

    # --- Channel helpers --------------------------------------------------

    @staticmethod
    def _channel_name(member: discord.abc.User) -> str:
        """A channel name derived from the member's name, safe for Discord."""
        base = "".join(ch for ch in member.name.lower() if ch.isalnum() or ch in "-_")
        return (base or f"user-{member.id}")[:MAX_CHANNEL_NAME]

    async def create_thread_channel(self, member: discord.abc.User) -> discord.TextChannel | None:
        """Create the private staff channel for a member."""
        guild = self.guild
        category = await self.get_category()
        if guild is None or category is None:
            return None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
                attach_files=True,
                embed_links=True,
            ),
        }

        try:
            channel = await guild.create_text_channel(
                name=self._channel_name(member),
                category=category,
                overwrites=overwrites,
                topic=f"ModMail thread with {member} ({member.id})",
                reason="ModMail thread opened",
            )
        except discord.Forbidden:
            logger.error("Missing permissions to create a thread channel.")
            return None

        await channel.send(
            embed=discord.Embed(
                title="New ModMail thread",
                description=(
                    f"{member.mention} (`{member.id}`)\n\n"
                    f"Reply in this channel to message them. "
                    f"Use `{self.settings.command_prefix}close` to end the thread."
                ),
                color=discord.Color.green(),
            )
        )
        return channel

    def _members_of_role(self, role_id: int) -> list[int]:
        """Member IDs holding a role, resolved live from the guild.

        Roles are looked up at mention time, so a renamed role keeps working.
        """
        guild = self.guild
        if guild is None:
            return []
        role = guild.get_role(role_id)
        if role is None:
            logger.warning("Configured mention role %s no longer exists.", role_id)
            return []
        return [member.id for member in role.members]

    # --- DM handling ------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        # Ignore our own messages and other bots.
        if message.author.bot or message.author.id == self.user.id:
            return

        if isinstance(message.channel, discord.DMChannel):
            # Handle the DM inline. The event fires once per message, so
            # ordering is already guaranteed; deferring to a background worker
            # only risks the work never running.
            try:
                await self.handle_dm(message)
            except Exception:
                logger.exception("Failed to handle DM from %s.", message.author.id)
            return

        # Staff messages in a thread channel are replies to the member. Slash
        # commands arrive through the interaction tree, not here, so nothing
        # needs to be filtered out.
        if isinstance(message.channel, discord.TextChannel):
            try:
                await self.handle_staff_message(message)
            except Exception:
                logger.exception("Staff reply relay failed in %s.", message.channel.id)
            return

    async def _drain_dms(self, recipient_id: int) -> None:
        queue = self._queues[recipient_id]
        try:
            while True:
                try:
                    # Wait for work instead of exiting when the queue looks
                    # empty: a message put between the emptiness check and the
                    # exit would otherwise be dropped and never relayed.
                    message = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    if queue.empty():
                        return
                    continue
                try:
                    await self.handle_dm(message)
                except Exception:
                    logger.exception("Failed to handle DM from %s.", recipient_id)
                finally:
                    queue.task_done()
        finally:
            self._workers.pop(recipient_id, None)

    async def handle_dm(self, message: discord.Message) -> None:
        """Route a member's DM to their thread, creating one if needed."""
        member = message.author
        thread = await self.threads.get_open_by_recipient(member.id)

        if thread is None:
            thread = await self._open_thread_for(member)
            if thread is None:
                await self._dm_failure(message)
                return

        channel = self.get_channel(thread.channel_id) if thread.channel_id else None
        if not isinstance(channel, discord.TextChannel):
            # The channel was deleted out from under us; reopen cleanly.
            logger.warning("Thread %s has no usable channel; reopening.", thread.id)
            await self.threads.close_thread(thread.id)
            thread = await self._open_thread_for(member)
            if thread is None:
                await self._dm_failure(message)
                return
            channel = self.get_channel(thread.channel_id)

        # Optional staff ping, decided before the send so a mention and its
        # message land in the same Discord message.
        mention_prefix = await self.mentions.mention_prefix(
            author_id=member.id,
            resolve_role_members=self._members_of_role,
            resolve_name=lambda uid: getattr(self.get_user(uid), "name", uid),
        )

        sent = await self.relay.to_staff(
            thread=thread,
            channel=channel,
            payload=RelayPayload.from_message(message),
            mention_prefix=mention_prefix,
        )

        # Acknowledge only once the message is confirmed in the staff channel,
        # so the tick always means "staff can see this".
        if sent is None:
            logger.error(
                "Not acknowledging DM %s from %s: relay to staff failed.",
                message.id,
                member.id,
            )
            return

        try:
            await message.add_reaction(DELIVERED_EMOJI)
        except discord.HTTPException:
            # The acknowledgement is cosmetic; a failure here must not affect
            # an already-successful relay.
            logger.warning("Could not add the delivered reaction to DM %s.", message.id)

    async def _open_thread_for(self, member: discord.abc.User):
        """Create the channel then the thread row, keeping them consistent."""
        channel = await self.create_thread_channel(member)
        if channel is None:
            return None
        try:
            thread = await self.threads.open_thread(
                member.id, channel_id=channel.id, recipient_name=str(member)
            )
        except ThreadAlreadyOpen:
            # Another message won the race; use that thread and drop this channel.
            logger.info("Thread for %s already open; removing duplicate channel.", member.id)
            await channel.delete(reason="Duplicate ModMail thread")
            return await self.threads.get_open_by_recipient(member.id)
        return thread

    async def _dm_failure(self, message: discord.Message) -> None:
        with contextlib.suppress(discord.HTTPException):
            await message.channel.send(
                "Sorry, ModMail could not reach the staff team. Please try again later."
            )

    # --- Staff replies ----------------------------------------------------

    async def handle_staff_message(self, message: discord.Message) -> bool:
        """Relay a staff message to the member. Returns True if it was relayed."""
        thread = await self.threads.get_open_by_channel(message.channel.id)
        if thread is None:
            return False

        recipient = self.get_user(thread.recipient_id)
        if recipient is None and self.guild is not None:
            # The member who opened the thread lives in our guild, so look
            # there before falling back to an API fetch, which returns None
            # for guild members.
            recipient = self.guild.get_member(thread.recipient_id)
        if recipient is None:
            try:
                recipient = await self.fetch_user(thread.recipient_id)
            except discord.HTTPException:
                logger.warning(
                    "Cannot resolve recipient %s for thread %s.", thread.recipient_id, thread.id
                )
                return False

        sent = await self.relay.to_user(
            thread=thread, recipient=recipient, payload=RelayPayload.from_message(message)
        )
        if sent is not None:
            with contextlib.suppress(discord.HTTPException):
                await message.add_reaction("\N{WHITE HEAVY CHECK MARK}")
        return sent is not None

    # --- Shutdown ---------------------------------------------------------

    async def close(self) -> None:
        for worker in list(self._workers.values()):
            worker.cancel()
        self._workers.clear()
        await super().close()


class ModmailCommands(commands.Cog):
    """All ModMail slash commands.

    Ticket commands work inside a thread channel; mention commands are
    administrative and work anywhere in the guild.
    """

    def __init__(self, bot: ModmailBot) -> None:
        self.bot = bot

    # --- Helpers ----------------------------------------------------------

    async def _current_thread(self, interaction: discord.Interaction):
        """The open thread for the channel the command was used in, if any."""
        channel_id = getattr(interaction.channel, "id", None)
        if channel_id is None:
            return None
        return await self.bot.threads.get_open_by_channel(channel_id)

    async def _resolve_member(self, user_id: int) -> discord.abc.User | None:
        """Look up a member, preferring the guild so departed users still work."""
        recipient = self.bot.get_user(user_id)
        if recipient is None and self.bot.guild is not None:
            recipient = self.bot.guild.get_member(user_id)
        if recipient is None:
            with contextlib.suppress(discord.HTTPException):
                recipient = await self.bot.fetch_user(user_id)
        return recipient

    @staticmethod
    def _is_authorised(interaction: discord.Interaction, role_id: int | None) -> bool:
        """Owner, administrator, or holder of the configured staff role."""
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        if member.guild.owner_id == member.id:
            return True
        permissions = member.guild_permissions
        if permissions.administrator or permissions.manage_guild:
            return True
        if role_id is not None:
            return any(role.id == role_id for role in member.roles)
        return False

    async def _require_thread(self, interaction: discord.Interaction):
        """Return the open thread or reply with an error and return None."""
        thread = await self._current_thread(interaction)
        if thread is None:
            await interaction.response.send_message(
                "This channel is not an open ModMail thread.", ephemeral=True
            )
            return None
        return thread

    # --- Ticket commands --------------------------------------------------

    @app_commands.command(name="help", description="List the ModMail commands.")
    async def help_command(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="ModMail commands",
            description="Members DM this bot to reach staff. Staff reply here.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Ticket commands",
            value=(
                "`/reply` — send a message to the member without posting it here\n"
                "`/close` — close this thread and archive it\n"
                "`/threadinfo` — show details about this thread\n"
                "`/help` — show this list"
            ),
            inline=False,
        )
        embed.add_field(
            name="Mention commands",
            value=(
                "`/mention on` — ping the configured staff on new messages\n"
                "`/mention off` — stop pinging staff\n"
                "`/mention user add` — add a staff member to the ping list\n"
                "`/mention user remove` — remove a staff member from the ping list\n"
                "`/mention role add` — ping every member of a role\n"
                "`/mention role remove` — stop pinging a role's members\n"
                "`/mention list` — show the current ping list"
            ),
            inline=False,
        )
        embed.add_field(
            name="Permissions",
            value=(
                "Mention commands require the server owner, an administrator, "
                "or the configured staff role."
            ),
            inline=False,
        )
        embed.set_footer(text="Type / in a thread channel to send a reply.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="reply", description="Send a message to the member without posting it here."
    )
    @app_commands.describe(message="The message to send to the member.")
    async def reply(self, interaction: discord.Interaction, message: str) -> None:
        thread = await self._require_thread(interaction)
        if thread is None:
            return

        recipient = await self._resolve_member(thread.recipient_id)
        if recipient is None:
            await interaction.response.send_message(
                "Could not resolve that member.", ephemeral=True
            )
            return

        payload = RelayPayload(
            content=message, author_id=interaction.user.id, author_name=str(interaction.user)
        )
        sent = await self.bot.relay.to_user(thread=thread, recipient=recipient, payload=payload)
        if sent is None:
            await interaction.response.send_message(
                "Could not deliver that message.", ephemeral=True
            )
            return
        await interaction.response.send_message("Sent to the member.", ephemeral=True)

    @app_commands.command(name="close", description="Close this thread and archive it.")
    @app_commands.describe(reason="Optional reason, shown to the member.")
    async def close(self, interaction: discord.Interaction, reason: str | None = None) -> None:
        thread = await self._require_thread(interaction)
        if thread is None:
            return

        await self.bot.threads.close_thread(thread.id, closed_by_id=interaction.user.id)

        recipient = await self._resolve_member(thread.recipient_id)
        if recipient is not None:
            embed = discord.Embed(
                title="Thread closed",
                description="Staff have closed this conversation. Message again to start a new one.",
                color=discord.Color.dark_grey(),
            )
            if reason:
                embed.add_field(name="Reason", value=reason[:1024], inline=False)
            with contextlib.suppress(discord.HTTPException):
                await recipient.send(embed=embed)

        await interaction.response.send_message(
            f"Thread `{thread.id}` closed." + (f" Reason: {reason}" if reason else "")
        )

        log_channel_id = self.bot.settings.log_channel_id
        log_channel = self.bot.get_channel(log_channel_id) if log_channel_id else None
        if isinstance(log_channel, discord.TextChannel):
            with contextlib.suppress(discord.HTTPException):
                await log_channel.send(
                    embed=discord.Embed(
                        title="Thread closed",
                        description=(
                            f"Thread `{thread.id}` with "
                            f"{thread.recipient_name or thread.recipient_id} "
                            f"was closed by {interaction.user.mention}."
                        ),
                        color=discord.Color.dark_grey(),
                    )
                )

        channel = interaction.channel
        if isinstance(channel, discord.TextChannel):
            with contextlib.suppress(discord.HTTPException):
                await channel.delete(reason=f"ModMail thread {thread.id} closed")

    @app_commands.command(name="threadinfo", description="Show details about this thread.")
    async def threadinfo(self, interaction: discord.Interaction) -> None:
        thread = await self._require_thread(interaction)
        if thread is None:
            return

        embed = discord.Embed(title=f"Thread {thread.id}", color=discord.Color.blurple())
        embed.add_field(name="Member", value=f"<@{thread.recipient_id}>")
        embed.add_field(name="Opened", value=discord.utils.format_dt(thread.created_at, "R"))
        embed.add_field(name="Messages", value=str(len(thread.messages)), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- Mention commands -------------------------------------------------

    async def _require_authorised(self, interaction: discord.Interaction) -> bool:
        if self._is_authorised(interaction, self.bot.settings.mention_admin_role_id):
            return True
        await interaction.response.send_message(
            "You need to be the server owner, an administrator, or hold the "
            "configured staff role to change mention settings.",
            ephemeral=True,
        )
        return False

    mention_group = app_commands.Group(
        name="mention", description="Configure who is pinged when a member writes in."
    )

    @mention_group.command(name="on", description="Ping configured staff on new messages.")
    async def mention_on(self, interaction: discord.Interaction) -> None:
        if not await self._require_authorised(interaction):
            return
        await self.bot.mentions.set_enabled(True)
        await interaction.response.send_message("\U0001f514 ModMail Mentions: ON")

    @mention_group.command(name="off", description="Stop pinging staff on new messages.")
    async def mention_off(self, interaction: discord.Interaction) -> None:
        if not await self._require_authorised(interaction):
            return
        await self.bot.mentions.set_enabled(False)
        await interaction.response.send_message("\U0001f515 ModMail Mentions: OFF")

    @mention_group.command(name="list", description="Show the current ping list.")
    async def mention_list(self, interaction: discord.Interaction) -> None:
        enabled = await self.bot.mentions.is_enabled()
        header = (
            "\U0001f514 ModMail Mentions: ON" if enabled else "\U0001f515 ModMail Mentions: OFF"
        )

        guild = interaction.guild
        role_ids = await self.bot.mentions.list_roles()
        user_ids = await self.bot.mentions.list_targets()

        role_lines = []
        for role_id in role_ids:
            role = guild.get_role(role_id) if guild else None
            # Deleted roles stay listed and are marked, rather than silently
            # vanishing from the configuration.
            role_lines.append(
                f"\u2022 {role.mention}" if role else f"\u2022 Unknown role (`{role_id}`)"
            )

        user_lines = []
        for user_id in user_ids:
            member = guild.get_member(user_id) if guild else None
            user_lines.append(
                f"\u2022 {member.mention}" if member else f"\u2022 Unknown member (`{user_id}`)"
            )

        parts = [
            header,
            "",
            "**Roles:**",
            *(role_lines or ["\u2022 None"]),
            "",
            "**Users:**",
            *(user_lines or ["\u2022 None"]),
        ]
        await interaction.response.send_message("\n".join(parts), ephemeral=True)

    # --- Mention: users ---------------------------------------------------

    user_group = app_commands.Group(
        name="user",
        description="Add or remove a staff member from the ping list.",
        parent=mention_group,
    )

    @user_group.command(name="add", description="Add a staff member to the ping list.")
    @app_commands.describe(user="The staff member to ping.")
    async def mention_user_add(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        if not await self._require_authorised(interaction):
            return
        added = await self.bot.mentions.add_target(user.id, added_by_id=interaction.user.id)
        if added:
            await interaction.response.send_message(
                f"Added {user.mention} to the ModMail mention list."
            )
        else:
            await interaction.response.send_message(
                f"{user.mention} is already in the ModMail mention list."
            )

    @user_group.command(name="remove", description="Remove a staff member from the ping list.")
    @app_commands.describe(user="The staff member to stop pinging.")
    async def mention_user_remove(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        if not await self._require_authorised(interaction):
            return
        removed = await self.bot.mentions.remove_target(user.id)
        if removed:
            await interaction.response.send_message(
                f"Removed {user.mention} from the ModMail mention list."
            )
        else:
            await interaction.response.send_message(
                f"{user.mention} is not in the ModMail mention list."
            )

    # --- Mention: roles ---------------------------------------------------

    role_group = app_commands.Group(
        name="role",
        description="Add or remove a role whose members are pinged.",
        parent=mention_group,
    )

    @role_group.command(name="add", description="Ping every member of a role.")
    @app_commands.describe(role="The role whose members should be pinged.")
    async def mention_role_add(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if not await self._require_authorised(interaction):
            return
        added = await self.bot.mentions.add_role(role.id, added_by_id=interaction.user.id)
        if added:
            await interaction.response.send_message(
                f"Added {role.mention} to the ModMail mention list."
            )
        else:
            await interaction.response.send_message(
                f"{role.mention} is already in the ModMail mention list."
            )

    @role_group.command(name="remove", description="Stop pinging a role's members.")
    @app_commands.describe(role="The role to stop pinging.")
    async def mention_role_remove(
        self, interaction: discord.Interaction, role: discord.Role
    ) -> None:
        if not await self._require_authorised(interaction):
            return
        removed = await self.bot.mentions.remove_role(role.id)
        if removed:
            await interaction.response.send_message(
                f"Removed {role.mention} from the ModMail mention list."
            )
        else:
            await interaction.response.send_message(
                f"{role.mention} is not in the ModMail mention list."
            )


async def handle_staff_reply(bot: ModmailBot, message: discord.Message) -> bool:
    """Entry point used by the bot for non-command staff messages."""
    return await bot.handle_staff_message(message)
