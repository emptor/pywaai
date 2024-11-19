import pytest
import asyncio
from datetime import datetime, timedelta
from pywaai.conversation_db import (
    ConnectionPool,
    ConversationHistory,
    ConversationManager,
    Conversation,
    Base
)

@pytest.fixture(scope="function")
async def test_instances():
    """Setup test instances."""
    # Setup phase - use shared memory database
    db_path = "file::memory:?cache=shared"
    pool = ConnectionPool(db_path, pool_size=2)
    history = ConversationHistory(db_path=db_path, pool_size=2)
    manager = ConversationManager(db_path=db_path, pool_size=2)

    # Initialize database through manager only
    await manager.init_db()

    # Drop and recreate tables
    session = await pool.get_connection()
    try:
        # Drop all tables
        Base.metadata.drop_all(pool.engine)
        # Recreate tables
        Base.metadata.create_all(pool.engine)
    finally:
        await pool.release_connection(session)

    yield pool, history, manager

    # Teardown phase
    await pool.close_all()
    await history.pool.close_all()
    await manager.history.pool.close_all()


@pytest.mark.asyncio
class TestConnectionPool:
    async def test_get_and_release_connection(self, test_instances):
        """Test getting and releasing connections from the pool."""
        async for pool, _, _ in test_instances:
            conn1 = await pool.get_connection()
            assert conn1 is not None
            await pool.release_connection(conn1)

            conn2 = await pool.get_connection()
            assert conn2 is not None
            await pool.release_connection(conn2)

    async def test_close_all(self, test_instances):
        """Test closing all connections in the pool."""
        async for pool, _, _ in test_instances:
            conn1 = await pool.get_connection()
            await pool.release_connection(conn1)
            await pool.close_all()

            # All connections should be closed
            for conn in pool.all_sessions:
                with pytest.raises(Exception):
                    conn.execute("SELECT 1")


@pytest.mark.asyncio
class TestConversationHistory:
    async def test_append_and_read_messages(self, test_instances):
        """Test appending and reading messages."""
        async for _, history, manager in test_instances:
            phone_number = "+1234567890"
            # Create a conversation first
            conversation = await manager.create_conversation(phone_number)
            message = {"role": "user", "content": "Hello"}
            await history.append(phone_number, message, conversation.conversation_id)
            messages = await history.read(phone_number, conversation.conversation_id)
            assert len(messages) == 1
            assert messages[0]["content"] == "Hello"

    async def test_multiple_conversations(self, test_instances):
        """Test handling multiple conversations for the same phone number."""
        async for _, history, manager in test_instances:
            phone_number = "+1234567890"
            # Create two conversations
            conv1 = await manager.create_conversation(phone_number)
            conv2 = await manager.create_conversation(phone_number)

            message1 = {"role": "user", "content": "Hello"}
            message2 = {"role": "user", "content": "Hi there"}

            await history.append(phone_number, message1, conv1.conversation_id)
            await history.append(phone_number, message2, conv2.conversation_id)

            conv1_messages = await history.read(phone_number, conv1.conversation_id)
            conv2_messages = await history.read(phone_number, conv2.conversation_id)

            assert len(conv1_messages) == 1
            assert len(conv2_messages) == 1
            assert conv1_messages[0]["content"] == "Hello"
            assert conv2_messages[0]["content"] == "Hi there"


@pytest.mark.asyncio
class TestConversationManager:
    async def test_create_conversation(self, test_instances):
        """Test creating a new conversation."""
        async for _, _, manager in test_instances:
            phone_number = "+1234567890"
            conversation = await manager.create_conversation(phone_number)
            assert conversation.phone_number == phone_number
            assert conversation.conversation_id is not None

    async def test_add_message_to_existing_conversation(self, test_instances):
        """Test adding messages to an existing conversation."""
        async for _, _, manager in test_instances:
            phone_number = "+1234567890"
            conversation = await manager.create_conversation(phone_number)
            message = {"role": "user", "content": "Hello"}
            await manager.add_message(phone_number, message, conversation.conversation_id)
            messages = await manager.get_messages(
                phone_number, conversation.conversation_id
            )
            assert len(messages) == 1
            assert messages[0]["content"] == "Hello"

    async def test_get_latest_conversation(self, test_instances):
        """Test getting the latest conversation."""
        async for _, _, manager in test_instances:
            phone_number = "+1234567890"
            conversation1 = await manager.create_conversation(phone_number)
            await asyncio.sleep(0.1)  # Ensure different timestamps
            conversation2 = await manager.create_conversation(phone_number)

            latest = await manager.get_latest_conversation(phone_number)
            assert latest.conversation_id == conversation2.conversation_id

    async def test_conversation_metadata(self, test_instances):
        """Test conversation metadata tracking."""
        async for _, _, manager in test_instances:
            phone_number = "+1234567890"
            conversation = await manager.create_conversation(phone_number)
            message = {"role": "user", "content": "Hello"}
            await manager.add_message(phone_number, message, conversation.conversation_id)

            conversations = await manager.get_conversations(phone_number)
            assert len(conversations) == 1
            assert len(conversations[0].messages) == 1
            assert conversations[0].phone_number == phone_number

    async def test_watch_conversation(self, test_instances):
        """Test watching conversation changes."""
        async for _, _, manager in test_instances:
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
