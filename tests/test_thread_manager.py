"""Thread lifecycle rules — the invariants the bot depends on."""

from __future__ import annotations

import pytest

from modmail.models import MessageDirection, ThreadStatus
from modmail.thread_manager import ThreadAlreadyOpen, ThreadNotFound


async def test_open_thread_creates_open_row(threads):
    thread = await threads.open_thread(111, channel_id=222, recipient_name="alice")
    assert thread.id is not None
    assert thread.status is ThreadStatus.OPEN
    assert thread.is_open is True


async def test_one_open_thread_per_recipient(threads):
    await threads.open_thread(111, channel_id=222)
    with pytest.raises(ThreadAlreadyOpen):
        await threads.open_thread(111, channel_id=333)


async def test_duplicate_check_is_enforced_by_the_database(database, threads):
    """Bypassing the manager must still fail: the partial index backs it up."""
    from sqlalchemy.exc import IntegrityError

    from modmail.models import Thread

    await threads.open_thread(111, channel_id=222)
    async with database.session_scope() as session:
        session.add(Thread(recipient_id=111, channel_id=333))
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


async def test_different_recipients_get_separate_threads(threads):
    first = await threads.open_thread(111, channel_id=222)
    second = await threads.open_thread(222, channel_id=333)
    assert first.id != second.id


async def test_lookup_by_recipient_and_channel(threads):
    thread = await threads.open_thread(111, channel_id=222)
    assert (await threads.get_open_by_recipient(111)).id == thread.id
    assert (await threads.get_open_by_channel(222)).id == thread.id
    assert await threads.get_open_by_recipient(999) is None
    assert await threads.get_open_by_channel(999) is None


async def test_set_channel_binds_after_creation(threads):
    thread = await threads.open_thread(111)
    assert thread.channel_id is None
    await threads.set_channel(thread.id, 222)
    assert (await threads.get(thread.id)).channel_id == 222


async def test_set_channel_on_missing_thread_raises(threads):
    with pytest.raises(ThreadNotFound):
        await threads.set_channel(4242, 222)


async def test_close_marks_closed_and_creates_log_entry(threads):
    thread = await threads.open_thread(111, channel_id=222)
    closed = await threads.close_thread(thread.id, closed_by_id=999)
    assert closed.status is ThreadStatus.CLOSED
    assert closed.closed_at is not None
    assert closed.closed_by_id == 999

    reloaded = await threads.get(thread.id)
    assert reloaded.log_entry is not None
    assert len(reloaded.log_entry.key) >= 20


async def test_closing_twice_is_idempotent(threads):
    thread = await threads.open_thread(111, channel_id=222)
    first = await threads.close_thread(thread.id, closed_by_id=999)
    assert first.log_entry is not None
    key = first.log_entry.key

    second = await threads.close_thread(thread.id, closed_by_id=999)
    assert second.status is ThreadStatus.CLOSED
    assert second.log_entry is not None
    assert second.log_entry.key == key


async def test_closing_missing_thread_raises(threads):
    with pytest.raises(ThreadNotFound):
        await threads.close_thread(4242)


async def test_closed_thread_frees_the_member_to_open_another(threads):
    first = await threads.open_thread(111, channel_id=222)
    await threads.close_thread(first.id, closed_by_id=999)

    assert await threads.get_open_by_recipient(111) is None
    second = await threads.open_thread(111, channel_id=333)
    assert second.id != first.id
    assert (await threads.get_open_by_channel(222)) is None


async def test_recording_messages_preserves_order_and_attachments(threads):
    thread = await threads.open_thread(111, channel_id=222)
    await threads.record_message(
        thread.id,
        direction=MessageDirection.TO_STAFF,
        author_id=111,
        content="first",
        attachment_urls=["https://cdn.example/a.png", "https://cdn.example/b.png"],
    )
    await threads.record_message(
        thread.id,
        direction=MessageDirection.TO_USER,
        author_id=999,
        content="second",
        staff_message_id=555,
    )

    reloaded = await threads.get(thread.id)
    assert [m.content for m in reloaded.messages] == ["first", "second"]
    assert reloaded.messages[0].attachments == [
        "https://cdn.example/a.png",
        "https://cdn.example/b.png",
    ]


async def test_staff_message_lookup_for_edit_sync(threads):
    thread = await threads.open_thread(111, channel_id=222)
    await threads.record_message(
        thread.id,
        direction=MessageDirection.TO_USER,
        author_id=999,
        content="hello",
        staff_message_id=555,
    )
    found = await threads.get_by_staff_message(555)
    assert found is not None
    assert found.content == "hello"
    assert await threads.get_by_staff_message(556) is None


async def test_log_key_retrieves_the_archived_thread(threads):
    thread = await threads.open_thread(111, channel_id=222)
    await threads.record_message(
        thread.id, direction=MessageDirection.TO_STAFF, author_id=111, content="archived"
    )
    closed = await threads.close_thread(thread.id, closed_by_id=999)
    key = closed.log_entry.key

    archived = await threads.get_log_by_key(key)
    assert archived is not None
    assert archived.id == thread.id
    assert archived.messages[0].content == "archived"
    assert await threads.get_log_by_key("not-a-real-key") is None
