import pytest
import asyncio
from datetime import datetime, timedelta
from pywaai.conversation_db import (
    ConnectionPool,
    ConversationHistory,
    ConversationManager,
    Conversation,
)

# Create shared instances at module level
pool = None
history = None
manager = None


@pytest.fixture(scope="function", autouse=True)
def setup_and_teardown():
    """Setup and teardown for all tests."""
    global pool, history, manager

    # Setup phase - use shared memory database
    db_path = "file::memory:?cache=shared"
    pool = ConnectionPool(db_path, pool_size=2)
    history = ConversationHistory(db_path=db_path, pool_size=2)
    manager = ConversationManager(db_path=db_path, pool_size=2)

    # Initialize database through manager only
    asyncio.run(manager.init_db())

    yield

    # Teardown phase
    asyncio.run(pool.close_all())
    asyncio.run(history.pool.close_all())
    asyncio.run(manager.history.pool.close_all())


@pytest.mark.asyncio
class TestConnectionPool:
    async def test_get_and_release_connection(self):
        """Test getting and releasing connections from the pool."""
        conn1 = await pool.get_connection()
        assert conn1 is not None
        await pool.release_connection(conn1)

        conn2 = await pool.get_connection()
        assert conn2 is not None
        await pool.release_connection(conn2)

    async def test_close_all(self):
        """Test closing all connections in the pool."""
        conn1 = await pool.get_connection()
        await pool.release_connection(conn1)
        await pool.close_all()

        # All connections should be closed
        for conn in pool.all_connections:
            with pytest.raises(Exception):
                conn.execute("SELECT 1")


@pytest.mark.asyncio
class TestConversationHistory:
    async def test_append_and_read_messages(self):
        """Test appending and reading messages."""
        phone_number = "+1234567890"
        message = {"role": "user", "content": "Hello"}
        await history.append(phone_number, message)
        messages = await history.read(phone_number)
        assert len(messages) == 1
        assert messages[0]["content"] == "Hello"

    async def test_multiple_conversations(self):
        """Test handling multiple conversations for the same phone number."""
        phone_number = "+1234567890"
        conv1_id = "conv1"
        conv2_id = "conv2"

        message1 = {"role": "user", "content": "Hello"}
        message2 = {"role": "user", "content": "Hi there"}

        await history.append(phone_number, message1, conv1_id)
        await history.append(phone_number, message2, conv2_id)

        conv1_messages = await history.read(phone_number, conv1_id)
        conv2_messages = await history.read(phone_number, conv2_id)

        assert len(conv1_messages) == 1
        assert len(conv2_messages) == 1
        assert conv1_messages[0]["content"] == "Hello"
        assert conv2_messages[0]["content"] == "Hi there"


@pytest.mark.asyncio
class TestConversationManager:
    async def test_create_conversation(self):
        """Test creating a new conversation."""
        phone_number = "+1234567890"
        conversation = await manager.create_conversation(phone_number)
        assert conversation.phone_number == phone_number
        assert conversation.conversation_id is not None

    async def test_add_message_to_existing_conversation(self):
        """Test adding messages to an existing conversation."""
        phone_number = "+1234567890"
        conversation = await manager.create_conversation(phone_number)
        message = {"role": "user", "content": "Hello"}
        await manager.add_message(phone_number, message, conversation.conversation_id)
        messages = await manager.get_messages(
            phone_number, conversation.conversation_id
        )
        assert len(messages) == 1
        assert messages[0]["content"] == "Hello"

    async def test_get_latest_conversation(self):
        """Test getting the latest conversation."""
        phone_number = "+1234567890"
        conversation1 = await manager.create_conversation(phone_number)
        await asyncio.sleep(0.1)  # Ensure different timestamps
        conversation2 = await manager.create_conversation(phone_number)

        latest = await manager.get_latest_conversation(phone_number)
        assert latest.conversation_id == conversation2.conversation_id

    async def test_conversation_metadata(self):
        """Test conversation metadata tracking."""
        phone_number = "+1234567890"
        conversation = await manager.create_conversation(phone_number)
        message = {"role": "user", "content": "Hello"}
        await manager.add_message(phone_number, message, conversation.conversation_id)

        conversations = await manager.get_conversations(phone_number)
        assert len(conversations) == 1
        assert conversations[0].message_count == 1
        assert conversations[0].phone_number == phone_number

    async def test_watch_conversation(self):
        """Test watching conversation changes."""
        phone_number = "+1234567890"
        conversation = await manager.create_conversation(phone_number)

        async def watch_conversation():
            changes = []
            async for change in manager.watch_conversation(
                phone_number, conversation.conversation_id
            ):
                changes.append(change)
                if len(changes) >= 1:
                    break
            return changes

        async def add_message():
            await asyncio.sleep(0.1)  # Give time for watch to start
            message = {"role": "user", "content": "Hello"}
            await manager.add_message(
                phone_number, message, conversation.conversation_id
            )

        async def run_test():
            watch_task = asyncio.create_task(watch_conversation())
            add_task = asyncio.create_task(add_message())
            changes = await watch_task
            await add_task
            return changes

        changes = await run_test()
        assert len(changes) == 1
        assert changes[0]["content"] == "Hello"
