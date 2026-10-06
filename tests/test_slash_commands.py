"""The slash command surface.

These tests assert the command tree that Discord will see, plus the permission
rule for the mention commands. They deliberately do not require a live
connection.
"""

from __future__ import annotations

import discord
import pytest

from modmail.bot import ModmailCommands

TOP_LEVEL = {"help", "reply", "close", "threadinfo", "mention"}


def _command_map() -> dict[str, object]:
    return {c.name: c for c in ModmailCommands.__cog_app_commands__}


def test_expected_top_level_commands_exist():
    assert set(_command_map()) == TOP_LEVEL


def test_no_prefix_commands_remain():
    """The old ? commands must be gone; only slash commands remain."""
    assert ModmailCommands.__cog_commands__ == []


def test_mention_subcommands_exist():
    group = _command_map()["mention"]
    assert {c.name for c in group.commands} == {"on", "off", "list", "user", "role"}


def test_mention_user_subcommands_exist():
    group = _command_map()["mention"]
    user_group = next(c for c in group.commands if c.name == "user")
    assert {c.name for c in user_group.commands} == {"add", "remove"}


def test_mention_role_subcommands_exist():
    group = _command_map()["mention"]
    role_group = next(c for c in group.commands if c.name == "role")
    assert {c.name for c in role_group.commands} == {"add", "remove"}


def test_reply_takes_a_required_message_parameter():
    reply = _command_map()["reply"]
    params = {p.name: p for p in reply.parameters}
    assert set(params) == {"message"}
    assert params["message"].required is True


def test_close_takes_an_optional_reason():
    close = _command_map()["close"]
    params = {p.name: p for p in close.parameters}
    assert set(params) == {"reason"}
    assert params["reason"].required is False


def test_mention_user_add_and_remove_take_a_user_parameter():
    group = _command_map()["mention"]
    user_group = next(c for c in group.commands if c.name == "user")
    subs = {c.name: c for c in user_group.commands}
    for name in ("add", "remove"):
        params = {p.name: p for p in subs[name].parameters}
        assert set(params) == {"user"}
        assert params["user"].required is True


def test_mention_role_add_and_remove_take_a_role_parameter():
    group = _command_map()["mention"]
    role_group = next(c for c in group.commands if c.name == "role")
    subs = {c.name: c for c in role_group.commands}
    for name in ("add", "remove"):
        params = {p.name: p for p in subs[name].parameters}
        assert set(params) == {"role"}
        assert params["role"].required is True


def test_mention_on_off_list_take_no_parameters():
    group = _command_map()["mention"]
    subs = {c.name: c for c in group.commands}
    for name in ("on", "off", "list"):
        assert list(subs[name].parameters) == []


def test_help_command_has_no_parameters():
    assert list(_command_map()["help"].parameters) == []


def test_threadinfo_has_no_parameters():
    assert list(_command_map()["threadinfo"].parameters) == []


# --- Permission rule ------------------------------------------------------


class FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


class FakePermissions:
    def __init__(self, *, administrator=False, manage_guild=False) -> None:
        self.administrator = administrator
        self.manage_guild = manage_guild


class FakeMember(discord.Member):
    """A real Member instance so isinstance checks behave as in production.

    The permission rule only reads three things, so they are set directly on
    the instance rather than going through Member.__init__, which needs a live
    gateway connection.
    """

    def __init__(self, member_id, *, owner_id=None, roles=(), **perms) -> None:
        object.__setattr__(self, "_id", member_id)
        object.__setattr__(self, "guild", type("G", (), {"owner_id": owner_id or 1})())
        object.__setattr__(self, "_guild_permissions", FakePermissions(**perms))
        object.__setattr__(self, "_roles", [FakeRole(r) for r in roles])

    @property
    def id(self):
        return self._id

    @property
    def guild_permissions(self):
        return self._guild_permissions

    @property
    def roles(self):
        return self._roles


class FakeInteraction:
    def __init__(self, member) -> None:
        self.user = member


@pytest.mark.parametrize(
    ("member", "role_id", "expected"),
    [
        (FakeMember(1, owner_id=1), None, True),
        (FakeMember(2, owner_id=1, administrator=True), None, True),
        (FakeMember(2, owner_id=1, manage_guild=True), None, True),
        (FakeMember(2, owner_id=1, roles=[55]), 55, True),
        (FakeMember(2, owner_id=1, roles=[56]), 55, False),
        (FakeMember(2, owner_id=1), None, False),
        (FakeMember(2, owner_id=1), 55, False),
    ],
)
def test_mention_authorisation_rule(member, role_id, expected):
    assert ModmailCommands._is_authorised(FakeInteraction(member), role_id) is expected


def test_plain_user_object_is_not_authorised():
    """A DM interaction has no Member, so it must never be authorised."""
    interaction = FakeInteraction(discord.Object(id=1))
    assert ModmailCommands._is_authorised(interaction, None) is False
