"""The configurable staff mention system."""

from __future__ import annotations

from conftest import FakeSendable
from modmail.mentions import MentionSettings
from modmail.relay import Relay, RelayPayload

STAFF_A = 900000000000000001
STAFF_B = 900000000000000002
MEMBER = 111


async def test_mentions_are_off_by_default(threads):
    settings = MentionSettings()
    assert await settings.is_enabled() is False
    assert await settings.list_targets() == []


async def test_enable_and_disable_persist(database):
    settings = MentionSettings()
    await settings.set_enabled(True)

    # A fresh instance reading the same database sees the change.
    assert await MentionSettings().is_enabled() is True

    await settings.set_enabled(False)
    assert await MentionSettings().is_enabled() is False


async def test_add_member(database):
    settings = MentionSettings()
    assert await settings.add_target(STAFF_A, added_by_id=1) is True
    assert await settings.list_targets() == [STAFF_A]


async def test_add_is_idempotent(database):
    settings = MentionSettings()
    await settings.add_target(STAFF_A)
    assert await settings.add_target(STAFF_A) is False
    assert await settings.list_targets() == [STAFF_A]


async def test_remove_member(database):
    settings = MentionSettings()
    await settings.add_target(STAFF_A)
    await settings.add_target(STAFF_B)

    assert await settings.remove_target(STAFF_A) is True
    assert await settings.list_targets() == [STAFF_B]


async def test_remove_unknown_member_reports_false(database):
    settings = MentionSettings()
    assert await settings.remove_target(STAFF_A) is False


async def test_multiple_members_persist_across_restart(database):
    settings = MentionSettings()
    await settings.set_enabled(True)
    await settings.add_target(STAFF_A)
    await settings.add_target(STAFF_B)

    # Simulate a restart: brand new objects, same database.
    restarted = MentionSettings()
    assert await restarted.is_enabled() is True
    assert await restarted.list_targets() == [STAFF_A, STAFF_B]


async def test_prefix_is_empty_when_disabled(database):
    settings = MentionSettings()
    await settings.add_target(STAFF_A)
    assert await settings.mention_prefix(author_id=MEMBER) == ""


async def test_prefix_is_empty_when_no_members(database):
    settings = MentionSettings()
    await settings.set_enabled(True)
    assert await settings.mention_prefix(author_id=MEMBER) == ""


async def test_prefix_mentions_all_members_once(database):
    settings = MentionSettings()
    await settings.set_enabled(True)
    await settings.add_target(STAFF_A)
    await settings.add_target(STAFF_B)

    prefix = await settings.mention_prefix(author_id=MEMBER)
    assert prefix == f"<@{STAFF_A}> <@{STAFF_B}>"
    assert prefix.count(f"<@{STAFF_A}>") == 1
    assert prefix.count(f"<@{STAFF_B}>") == 1


async def test_prefix_never_mentions_the_ticket_owner(database):
    settings = MentionSettings()
    await settings.set_enabled(True)
    await settings.add_target(STAFF_A)
    await settings.add_target(MEMBER)  # the member who opened the thread

    prefix = await settings.mention_prefix(author_id=MEMBER)
    assert prefix == f"<@{STAFF_A}>"
    assert f"<@{MEMBER}>" not in prefix


async def test_prefix_empty_when_only_the_owner_is_configured(database):
    settings = MentionSettings()
    await settings.set_enabled(True)
    await settings.add_target(MEMBER)
    assert await settings.mention_prefix(author_id=MEMBER) == ""


async def test_prefix_never_uses_everyone_or_here(database):
    settings = MentionSettings()
    await settings.set_enabled(True)
    await settings.add_target(STAFF_A)

    prefix = await settings.mention_prefix(author_id=MEMBER)
    assert "@everyone" not in prefix
    assert "@here" not in prefix


# --- Integration with the relay -------------------------------------------


async def test_relay_prepends_mentions_when_enabled(threads):
    settings = MentionSettings()
    await settings.set_enabled(True)
    await settings.add_target(STAFF_A)

    thread = await threads.open_thread(MEMBER, channel_id=222, recipient_name="alice")
    channel = FakeSendable(222)
    prefix = await settings.mention_prefix(author_id=MEMBER)

    await Relay(threads).to_staff(
        thread=thread,
        channel=channel,
        payload=RelayPayload(content="help", author_id=MEMBER),
        mention_prefix=prefix,
    )

    assert channel.last_content == f"<@{STAFF_A}>"
    assert channel.sent[0]["embed"].description == "help"


async def test_relay_content_is_unchanged_when_mentions_off(threads):
    settings = MentionSettings()
    thread = await threads.open_thread(MEMBER, channel_id=222, recipient_name="alice")
    channel = FakeSendable(222)

    prefix = await settings.mention_prefix(author_id=MEMBER)
    await Relay(threads).to_staff(
        thread=thread,
        channel=channel,
        payload=RelayPayload(content="help", author_id=MEMBER),
        mention_prefix=prefix,
    )

    # Exactly as before the feature existed: no extra content.
    assert channel.last_content is None
    assert channel.sent[0]["embed"].description == "help"


async def test_relay_default_mention_prefix_keeps_old_behaviour(threads):
    thread = await threads.open_thread(MEMBER, channel_id=222, recipient_name="alice")
    channel = FakeSendable(222)

    await Relay(threads).to_staff(
        thread=thread, channel=channel, payload=RelayPayload(content="help", author_id=MEMBER)
    )

    assert channel.last_content is None


async def test_messages_are_still_recorded_with_mentions(threads):
    settings = MentionSettings()
    await settings.set_enabled(True)
    await settings.add_target(STAFF_A)

    thread = await threads.open_thread(MEMBER, channel_id=222, recipient_name="alice")
    channel = FakeSendable(222)
    prefix = await settings.mention_prefix(author_id=MEMBER)

    await Relay(threads).to_staff(
        thread=thread,
        channel=channel,
        payload=RelayPayload(content="help", author_id=MEMBER),
        mention_prefix=prefix,
    )

    recorded = (await threads.get(thread.id)).messages
    assert len(recorded) == 1
    assert recorded[0].content == "help"  # mentions are not part of the archive


# --- Roles ----------------------------------------------------------------

ROLE_A = 800000000000000001
ROLE_B = 800000000000000002

# Simple guild stand-ins: role id -> member ids.
ROLE_MEMBERS = {
    ROLE_A: [STAFF_A, MEMBER],  # MEMBER also holds the role
    ROLE_B: [STAFF_B],
}


def resolve_role_members(role_id: int) -> list[int]:
    return ROLE_MEMBERS.get(role_id, [])


async def test_role_add_and_list(database):
    settings = MentionSettings()
    assert await settings.add_role(ROLE_A, added_by_id=1) is True
    assert await settings.list_roles() == [ROLE_A]


async def test_role_add_is_idempotent(database):
    settings = MentionSettings()
    await settings.add_role(ROLE_A)
    assert await settings.add_role(ROLE_A) is False
    assert await settings.list_roles() == [ROLE_A]


async def test_role_remove(database):
    settings = MentionSettings()
    await settings.add_role(ROLE_A)
    await settings.add_role(ROLE_B)
    assert await settings.remove_role(ROLE_A) is True
    assert await settings.list_roles() == [ROLE_B]
    assert await settings.remove_role(ROLE_A) is False


async def test_users_and_roles_persist_together(database):
    settings = MentionSettings()
    await settings.set_enabled(True)
    await settings.add_target(STAFF_A)
    await settings.add_role(ROLE_B)

    restarted = MentionSettings()
    assert await restarted.is_enabled() is True
    assert await restarted.list_targets() == [STAFF_A]
    assert await restarted.list_roles() == [ROLE_B]


async def test_role_members_are_mentioned(database):
    settings = MentionSettings()
    await settings.set_enabled(True)
    await settings.add_role(ROLE_B)

    prefix = await settings.mention_prefix(
        author_id=MEMBER, resolve_role_members=resolve_role_members
    )
    assert prefix == f"<@{STAFF_B}>"


async def test_direct_user_and_role_member_are_deduplicated(database):
    """STAFF_A is both directly configured and a member of ROLE_A."""
    settings = MentionSettings()
    await settings.set_enabled(True)
    await settings.add_target(STAFF_A)
    await settings.add_role(ROLE_A)

    ids = await settings.resolve_target_ids(
        author_id=MEMBER, resolve_role_members=resolve_role_members
    )
    assert ids.count(STAFF_A) == 1
    assert MEMBER not in ids  # role member, but also the ticket opener


async def test_ticket_opener_never_mentioned_via_role(database):
    settings = MentionSettings()
    await settings.set_enabled(True)
    await settings.add_role(ROLE_A)  # contains MEMBER and STAFF_A

    prefix = await settings.mention_prefix(
        author_id=MEMBER, resolve_role_members=resolve_role_members
    )
    assert f"<@{MEMBER}>" not in prefix
    assert prefix == f"<@{STAFF_A}>"


async def test_multiple_users_and_roles_all_mentioned_once(database):
    settings = MentionSettings()
    await settings.set_enabled(True)
    await settings.add_target(STAFF_A)
    await settings.add_role(ROLE_B)

    prefix = await settings.mention_prefix(
        author_id=MEMBER, resolve_role_members=resolve_role_members
    )
    assert prefix.count(f"<@{STAFF_A}>") == 1
    assert prefix.count(f"<@{STAFF_B}>") == 1


async def test_roles_never_produce_everyone_or_here(database):
    settings = MentionSettings()
    await settings.set_enabled(True)
    await settings.add_role(ROLE_A)

    prefix = await settings.mention_prefix(
        author_id=MEMBER, resolve_role_members=resolve_role_members
    )
    assert "@everyone" not in prefix
    assert "@here" not in prefix
    # No role pings either: they would bypass the ticket-opener rule.
    assert "<@&" not in prefix


async def test_role_mentions_off_produces_nothing(database):
    settings = MentionSettings()
    await settings.add_role(ROLE_A)
    assert (
        await settings.mention_prefix(author_id=MEMBER, resolve_role_members=resolve_role_members)
        == ""
    )


async def test_missing_role_resolves_to_nothing(database):
    settings = MentionSettings()
    await settings.set_enabled(True)
    await settings.add_role(999999)
    assert (
        await settings.mention_prefix(author_id=MEMBER, resolve_role_members=resolve_role_members)
        == ""
    )


async def test_relay_contains_real_user_mention_syntax_with_roles(threads):
    """Regression: the outgoing content must contain <@USER_ID>."""
    settings = MentionSettings()
    await settings.set_enabled(True)
    await settings.add_target(STAFF_A)
    await settings.add_role(ROLE_B)

    thread = await threads.open_thread(MEMBER, channel_id=222, recipient_name="alice")
    channel = FakeSendable(222)
    prefix = await settings.mention_prefix(
        author_id=MEMBER, resolve_role_members=resolve_role_members
    )

    await Relay(threads).to_staff(
        thread=thread,
        channel=channel,
        payload=RelayPayload(content="help", author_id=MEMBER),
        mention_prefix=prefix,
    )

    assert f"<@{STAFF_A}>" in channel.last_content
    assert f"<@{STAFF_B}>" in channel.last_content
    assert channel.sent[0]["embed"].description == "help"
