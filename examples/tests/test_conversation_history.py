import pytest
import asyncio
import tempfile
import os
import json
import sqlite3
from cachetools import TTLCache
from unittest import mock
from db_utils import ConversationHistory


# Pytest fixtures and test cases
@pytest.fixture
def conversation_history():
    # Create a temporary database file
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        db_path = tmp.name

    # Create an instance of ConversationHistory
    conv_history = ConversationHistory(db_path=db_path, pool_size=2)

    yield conv_history

    # Cleanup: delete the temporary database file
    os.unlink(db_path)


@pytest.mark.asyncio
async def test_init_db(conversation_history):
    # Test that the database is initialized correctly
    conn = sqlite3.connect(conversation_history.db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'"
    )
    table_exists = cursor.fetchone()
    conn.close()
    assert table_exists is not None, "conversations table should exist"


@pytest.mark.asyncio
async def test_set_and_get_item(conversation_history):
    phone_number = "1234567890"
    messages = [{"role": "user", "content": "Hello"}]

    await conversation_history.__setitem__(phone_number, messages)
    retrieved_messages = await conversation_history.__getitem__(phone_number)
    assert retrieved_messages == messages


@pytest.mark.asyncio
async def test_append(conversation_history):
    phone_number = "1234567890"
    message1 = {"role": "user", "content": "Hello"}
    message2 = {"role": "assistant", "content": "Hi there!"}

    await conversation_history.append(phone_number, message1)
    await conversation_history.append(phone_number, message2)

    retrieved_messages = await conversation_history.__getitem__(phone_number)
    assert retrieved_messages == [message1, message2]


@pytest.mark.asyncio
async def test_cache(conversation_history):
    phone_number = "1234567890"
    messages = [{"role": "user", "content": "Hello"}]

    await conversation_history.__setitem__(phone_number, messages)
    # Access the cache directly to ensure that messages are cached
    assert phone_number in conversation_history.message_cache
    cached_messages = conversation_history.message_cache[phone_number]
    assert cached_messages == messages

    # Now, delete the messages from the database
    conn = sqlite3.connect(conversation_history.db_path)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM conversations WHERE phone_number=?", (phone_number,))
    conn.commit()
    conn.close()

    # Retrieve messages again; should come from cache
    retrieved_messages = await conversation_history.__getitem__(phone_number)
    assert retrieved_messages == messages


@pytest.mark.asyncio
async def test_read(capsys, conversation_history):
    phone_number = "1234567890"
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    await conversation_history.__setitem__(phone_number, messages)

    conversation_history.read(phone_number)

    captured = capsys.readouterr()
    assert "User: Hello" in captured.out
    assert "Assistant: Hi there!" in captured.out


@pytest.mark.asyncio
async def test_watch(conversation_history):
    phone_number = "1234567890"
    messages = [{"role": "user", "content": "Hello"}]
    await conversation_history.__setitem__(phone_number, messages)

    with mock.patch("builtins.print") as mock_print:
        # Start watching in a background task
        async def watch_coroutine():
            await conversation_history.watch(phone_number)

        watch_task = asyncio.create_task(watch_coroutine())

        # Wait a bit to let the watch start
        await asyncio.sleep(0.1)

        # Append a new message
        new_message = {"role": "assistant", "content": "Hi there!"}
        await conversation_history.append(phone_number, new_message)

        # Wait a bit to allow the watch to pick up the new message
        await asyncio.sleep(2)

        # Cancel the watch task
        watch_task.cancel()
        try:
            await watch_task
        except asyncio.CancelledError:
            pass

        # Check that the new message was printed
        mock_print.assert_called_with("ASSISTANT: Hi there!")
