"""Relay behaviour: direction, content, attachments, and failure handling."""

from __future__ import annotations

from conftest import FakeMessage, FakeSendable
from modmail.models import MessageDirection
from modmail.relay import Relay, RelayPayload, build_staff_embed, build_user_embed


def test_payload_from_message_flattens_fields(author):
    message = FakeMessage(author=author, content="hello", attachments=["https://x/a.png"])
    payload = RelayPayload.from_message(message)
    assert payload.content == "hello"
    assert payload.attachments == ["https://x/a.png"]
    assert payload.author_id == 111
    assert payload.message_id == 1


def test_empty_payload_detection(author):
    assert RelayPayload(content="   ").is_empty is True
    assert RelayPayload(content="", attachments=["https://x/a.png"]).is_empty is False
    assert RelayPayload(content="hi").is_empty is False


def test_long_content_is_truncated_to_discord_limit():
    payload = RelayPayload(content="x" * 2500)
    assert len(payload.truncated) == 2000
    assert payload.truncated.endswith("...")


def test_embeds_are_built_for_both_directions():
    payload = RelayPayload(content="hi", attachments=["https://x/a.png"])
    staff_embed = build_staff_embed(payload, recipient_name="alice")
    user_embed = build_user_embed(payload)
    assert staff_embed.description == "hi"
    assert staff_embed.author.name == "alice (member)"
    assert user_embed.author.name == "ModMail Staff"
    assert staff_embed.fields[0].name == "Attachments"


def test_user_embed_hides_staff_identity():
    """Regression: the member-facing embed must never leak staff identity."""
    payload = RelayPayload(
        content="hello",
        attachments=["https://x/a.png"],
        author_id=999,
        author_name="piyushhkp",
    )
    user_embed = build_user_embed(payload)
    assert user_embed.author.name == "ModMail Staff"
    assert "piyushhkp" not in user_embed.author.name
    assert "999" not in user_embed.author.name
    assert user_embed.description == "hello"
    assert user_embed.fields[0].value == "https://x/a.png"


def test_staff_embed_still_shows_the_member_internally():
    """The staff channel keeps seeing who the member is (unchanged)."""
    payload = RelayPayload(content="help", author_id=111, author_name="alice")
    staff_embed = build_staff_embed(payload, recipient_name="alice")
    assert "alice" in staff_embed.author.name
    assert "member" in staff_embed.author.name.lower()


async def test_member_message_is_relayed_and_recorded(threads):
    relay = Relay(threads)
    thread = await threads.open_thread(111, channel_id=222, recipient_name="alice")
    channel = FakeSendable(222)

    payload = RelayPayload(
        content="I need help",
        attachments=["https://x/a.png"],
        author_id=111,
        author_name="alice",
        message_id=7,
    )
    sent = await relay.to_staff(thread=thread, channel=channel, payload=payload)

    assert sent is not None
    assert len(channel.sent) == 1
    assert channel.sent[0]["embed"].description == "I need help"

    recorded = (await threads.get(thread.id)).messages[0]
    assert recorded.direction is MessageDirection.TO_STAFF
    assert recorded.dm_message_id == 7
    assert recorded.staff_message_id == sent.id


async def test_staff_message_is_relayed_and_recorded(threads):
    relay = Relay(threads)
    thread = await threads.open_thread(111, channel_id=222, recipient_name="alice")
    recipient = FakeSendable(111)

    payload = RelayPayload(content="On it", author_id=999, author_name="moderator", message_id=9)
    sent = await relay.to_user(thread=thread, recipient=recipient, payload=payload)

    assert sent is not None
    assert recipient.sent[0]["embed"].description == "On it"

    recorded = (await threads.get(thread.id)).messages[0]
    assert recorded.direction is MessageDirection.TO_USER
    assert recorded.staff_message_id == 9


async def test_empty_message_is_not_sent_or_recorded(threads):
    relay = Relay(threads)
    thread = await threads.open_thread(111, channel_id=222)
    channel = FakeSendable(222)

    sent = await relay.to_staff(thread=thread, channel=channel, payload=RelayPayload(content="  "))

    assert sent is None
    assert channel.sent == []
    assert (await threads.get(thread.id)).messages == []


async def test_forbidden_send_is_swallowed(threads):
    """A closed DM channel must not crash the bot."""
    relay = Relay(threads)
    thread = await threads.open_thread(111, channel_id=222)
    recipient = FakeSendable(111, forbidden=True)

    sent = await relay.to_user(
        thread=thread, recipient=recipient, payload=RelayPayload(content="hi", author_id=999)
    )

    assert sent is None
    assert (await threads.get(thread.id)).messages == []


async def test_conversation_alternates_directions(threads):
    relay = Relay(threads)
    thread = await threads.open_thread(111, channel_id=222, recipient_name="alice")
    staff_channel = FakeSendable(222)
    dm = FakeSendable(111)

    await relay.to_staff(
        thread=thread, channel=staff_channel, payload=RelayPayload(content="help", author_id=111)
    )
    await relay.to_user(
        thread=thread, recipient=dm, payload=RelayPayload(content="sure", author_id=999)
    )
    await relay.to_staff(
        thread=thread, channel=staff_channel, payload=RelayPayload(content="thanks", author_id=111)
    )

    recorded = (await threads.get(thread.id)).messages
    assert [m.direction for m in recorded] == [
        MessageDirection.TO_STAFF,
        MessageDirection.TO_USER,
        MessageDirection.TO_STAFF,
    ]


async def test_staff_to_user_relay_hides_identity_and_keeps_attachments(threads):
    """Staff -> user relay: generic label, attachments intact, archive intact."""
    relay = Relay(threads)
    thread = await threads.open_thread(111, channel_id=222, recipient_name="alice")
    recipient = FakeSendable(111)

    payload = RelayPayload(
        content="On it",
        attachments=["https://x/a.png", "https://x/b.png"],
        author_id=999,
        author_name="._hiki",
        message_id=9,
    )
    sent = await relay.to_user(thread=thread, recipient=recipient, payload=payload)

    assert sent is not None
    embed = recipient.sent[0]["embed"]
    # The member sees the generic label only.
    assert embed.author.name == "ModMail Staff"
    assert "._hiki" not in embed.author.name
    assert "999" not in embed.author.name
    # Attachments still rendered.
    assert embed.fields[0].name == "Attachments"
    assert "https://x/a.png" in embed.fields[0].value
    assert "https://x/b.png" in embed.fields[0].value

    # The archive still records the real staff identity internally.
    recorded = (await threads.get(thread.id)).messages[0]
    assert recorded.direction is MessageDirection.TO_USER
    assert recorded.author_id == 999
    assert recorded.author_name == "._hiki"
    assert recorded.content == "On it"
