import pytest
import asyncio
import tempfile
import os
import json
from unittest import mock
from db_utils import EncryptedConversationHistory


# Pytest fixtures and test cases
@pytest.fixture
def encrypted_conversation_history():
    # Create temporary database files
    with (
        tempfile.NamedTemporaryFile(delete=False) as tmp_db,
        tempfile.NamedTemporaryFile(delete=False) as tmp_salt_db,
    ):
        db_path = tmp_db.name
        salt_db_path = tmp_salt_db.name

    # Create an instance of EncryptedConversationHistory
    master_key = "test_master_key"
    salt_master_key = "test_salt_master_key"
    conv_history = EncryptedConversationHistory(
        db_path=db_path,
        salt_db_path=salt_db_path,
        master_key=master_key,
        salt_master_key=salt_master_key,
        pool_size=2,
    )

    yield conv_history

    # Cleanup: delete the temporary database files
    os.unlink(db_path)
    os.unlink(salt_db_path)


@pytest.mark.asyncio
async def test_init_db(encrypted_conversation_history):
    # Test that the database is initialized correctly
    conn = await encrypted_conversation_history.pool.get_connection()
    try:
        conn.execute(f"PRAGMA key = '{encrypted_conversation_history.master_key}';")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'"
        )
        table_exists = cursor.fetchone()
        assert table_exists is not None, "conversations table should exist"
    finally:
        await encrypted_conversation_history.pool.release_connection(conn)


@pytest.mark.asyncio
async def test_set_and_get_item(encrypted_conversation_history):
    phone_number = "1234567890"
    messages = [{"role": "user", "content": "Hello"}]

    await encrypted_conversation_history.__setitem__(phone_number, messages)
    retrieved_messages = await encrypted_conversation_history.__getitem__(phone_number)
    assert retrieved_messages == messages


@pytest.mark.asyncio
async def test_append(encrypted_conversation_history):
    phone_number = "1234567890"
    message1 = {"role": "user", "content": "Hello"}
    message2 = {"role": "assistant", "content": "Hi there!"}

    await encrypted_conversation_history.append(phone_number, message1)
    await encrypted_conversation_history.append(phone_number, message2)

    retrieved_messages = await encrypted_conversation_history.__getitem__(phone_number)
    assert retrieved_messages == [message1, message2]


@pytest.mark.asyncio
async def test_cache(encrypted_conversation_history):
    phone_number = "1234567890"
    messages = [{"role": "user", "content": "Hello"}]

    await encrypted_conversation_history.__setitem__(phone_number, messages)
    # Access the cache directly to ensure that messages are cached
    assert phone_number in encrypted_conversation_history.message_cache
    cached_messages = encrypted_conversation_history.message_cache[phone_number]
    assert cached_messages == messages

    # Retrieve messages again; should come from cache
    retrieved_messages = await encrypted_conversation_history.__getitem__(phone_number)
    assert retrieved_messages == messages


@pytest.mark.asyncio
async def test_read(capsys, encrypted_conversation_history):
    phone_number = "1234567890"
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    await encrypted_conversation_history.__setitem__(phone_number, messages)

    await encrypted_conversation_history.read(phone_number)

    captured = capsys.readouterr()
    assert "User: Hello" in captured.out
    assert "Assistant: Hi there!" in captured.out


@pytest.mark.asyncio
async def test_watch(encrypted_conversation_history):
    phone_number = "1234567890"
    messages = [{"role": "user", "content": "Hello"}]
    await encrypted_conversation_history.__setitem__(phone_number, messages)

    with mock.patch("builtins.print") as mock_print:
        # Start watching in a background task
        async def watch_coroutine():
            await encrypted_conversation_history.watch(phone_number)

        watch_task = asyncio.create_task(watch_coroutine())

        # Wait a bit to let the watch start
        await asyncio.sleep(0.1)

        # Append a new message
        new_message = {"role": "assistant", "content": "Hi there!"}
        await encrypted_conversation_history.append(phone_number, new_message)

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


@pytest.mark.asyncio
async def test_encryption(encrypted_conversation_history):
    phone_number = "1234567890"
    message = {"role": "user", "content": "Secret message"}

    # Set the message
    await encrypted_conversation_history.__setitem__(phone_number, [message])

    # Check that the message is encrypted in the database
    conn = await encrypted_conversation_history.pool.get_connection()
    try:
        conn.execute(f"PRAGMA key = '{encrypted_conversation_history.master_key}';")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT encrypted_message FROM conversations WHERE phone_number=?",
            (phone_number,),
        )
        encrypted_message = cursor.fetchone()[0]
        assert encrypted_message != json.dumps(message), "Message should be encrypted"
    finally:
        await encrypted_conversation_history.pool.release_connection(conn)

    # Retrieve the message and check if it's decrypted correctly
    retrieved_messages = await encrypted_conversation_history.__getitem__(phone_number)
    assert retrieved_messages == [
        message
    ], "Retrieved message should match the original"


@pytest.mark.asyncio
async def test_salt_generation(encrypted_conversation_history):
    phone_number1 = "1234567890"
    phone_number2 = "9876543210"

    salt1 = await encrypted_conversation_history.get_or_create_salt(phone_number1)
    salt2 = await encrypted_conversation_history.get_or_create_salt(phone_number2)

    assert salt1 != salt2, "Salts for different phone numbers should be different"

    # Check if the same salt is returned for the same phone number
    salt1_again = await encrypted_conversation_history.get_or_create_salt(phone_number1)
    assert salt1 == salt1_again, "Salt should be consistent for the same phone number"


@pytest.mark.asyncio
async def test_key_derivation(encrypted_conversation_history):
    phone_number1 = "1234567890"
    phone_number2 = "9876543210"

    key1 = await encrypted_conversation_history.derive_key(phone_number1)
    key2 = await encrypted_conversation_history.derive_key(phone_number2)

    assert key1 != key2, "Derived keys for different phone numbers should be different"

    # Check if the same key is returned for the same phone number
    key1_again = await encrypted_conversation_history.derive_key(phone_number1)
    assert (
        key1 == key1_again
    ), "Derived key should be consistent for the same phone number"
