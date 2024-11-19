import pytest
import asyncio
from datetime import datetime, timedelta
from pywaai.conversation_db import (
    ConnectionPool,
    ConversationHistory,
    ConversationManager,
    Conversation,
    Base,
    EncryptedConversationHistory
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


@pytest.fixture(scope="function")
async def encrypted_test_instances():
    """Setup encrypted test instances."""
    db_path = "file::memory:?cache=shared"
    # Using a 32-byte key (256 bits) which is valid for AESGCM
    master_key = "x" * 32  # 32 bytes for the master key
    salt_master_key = "y" * 16  # 16 bytes for the salt
    
    pool = ConnectionPool(db_path, pool_size=2)
    history = EncryptedConversationHistory(
        db_path=db_path,
        pool_size=2,
        master_key=master_key,
        salt_master_key=salt_master_key
    )
    manager = ConversationManager(db_path=db_path, pool_size=2, history=history)

    await manager.init_db()

    session = await pool.get_connection()
    try:
        Base.metadata.drop_all(pool.engine)
        Base.metadata.create_all(pool.engine)
    finally:
        await pool.release_connection(session)

    yield pool, history, manager

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


@pytest.mark.asyncio
class TestEncryptedConversationHistory:
    async def test_message_encryption_decryption(self, encrypted_test_instances):
        """Test that messages are properly encrypted and decrypted."""
        async for _, history, manager in encrypted_test_instances:
            phone_number = "+1234567890"
            conversation = await manager.create_conversation(phone_number)
            
            original_message = {
                "role": "user",
                "content": "This is a secret message",
                "metadata": {"timestamp": "2023-01-01T00:00:00"}
            }
            
            # Test encryption
            encrypted = history._encrypt_message(original_message)
            assert encrypted != original_message
            assert "encrypted" in encrypted  # Check for encrypted content
            assert "nonce" in encrypted     # Check for nonce
            assert isinstance(encrypted["encrypted"], str)  # Should be base64 encoded
            assert isinstance(encrypted["nonce"], str)      # Should be base64 encoded
            
            # Test decryption
            decrypted = history._decrypt_message(encrypted)
            assert decrypted == original_message

    async def test_append_and_read_encrypted(self, encrypted_test_instances):
        """Test appending and reading encrypted messages."""
        async for _, history, manager in encrypted_test_instances:
            phone_number = "+1234567890"
            conversation = await manager.create_conversation(phone_number)
            
            message = {
                "role": "user",
                "content": "Secret message 1",
                "metadata": {"timestamp": "2023-01-01T00:00:00"}
            }
            
            await history.append(phone_number, message, conversation.conversation_id)
            messages = await history.read(phone_number, conversation.conversation_id)
            
            assert len(messages) == 1
            assert messages[0]["content"] == message["content"]
            assert messages[0]["role"] == message["role"]
            assert messages[0]["metadata"] == message["metadata"]

    async def test_watch_encrypted_conversation(self, encrypted_test_instances):
        """Test watching encrypted conversation changes."""
        async for _, history, manager in encrypted_test_instances:
            phone_number = "+1234567890"
            conversation = await manager.create_conversation(phone_number)
            
            # Start watching the conversation
            messages = []
            async def watch_and_collect():
                async for message in history.watch(phone_number, conversation.conversation_id):
                    messages.append(message)
            
            watch_task = asyncio.create_task(watch_and_collect())
            
            # Give the watch task time to start
            await asyncio.sleep(0.1)
            
            # Add messages with timestamps
            now = datetime.utcnow().isoformat()
            message1 = {
                "role": "user",
                "content": "Secret 1",
                "timestamp": now
            }
            await history.append(phone_number, message1, conversation.conversation_id)
            
            # Wait a bit before sending the second message
            await asyncio.sleep(0.1)
            now = datetime.utcnow().isoformat()
            message2 = {
                "role": "assistant",
                "content": "Secret 2",
                "timestamp": now
            }
            await history.append(phone_number, message2, conversation.conversation_id)
            
            # Wait for messages to be processed and session to refresh
            await asyncio.sleep(1.5)  # Wait longer than the refresh interval
            
            # Cancel the watch task
            watch_task.cancel()
            try:
                await watch_task
            except asyncio.CancelledError:
                pass
            
            # Verify messages
            assert len(messages) >= 2, f"Expected at least 2 messages, got {len(messages)}"
            assert messages[0]["content"] == "Secret 1"
            assert messages[1]["content"] == "Secret 2"
            # Verify timestamps are preserved
            assert "timestamp" in messages[0]
            assert "timestamp" in messages[1]

    async def test_different_encryption_keys(self, encrypted_test_instances):
        """Test that different encryption keys produce different results."""
        async for _, history, _ in encrypted_test_instances:
            # Create a second history instance with different keys
            different_history = EncryptedConversationHistory(
                db_path="file::memory:?cache=shared",
                master_key="z" * 32,  # Different 32-byte key
                salt_master_key="w" * 16  # Different 16-byte salt
            )
            
            message = {"role": "user", "content": "Test message"}
            
            # Encrypt with both instances
            encrypted1 = history._encrypt_message(message)
            encrypted2 = different_history._encrypt_message(message)
            
            # The encrypted content should be different
            assert encrypted1["encrypted"] != encrypted2["encrypted"]
            
            # But both should decrypt to the original message with their respective keys
            decrypted1 = history._decrypt_message(encrypted1)
            decrypted2 = different_history._decrypt_message(encrypted2)
            assert decrypted1["content"] == message["content"]
            assert decrypted2["content"] == message["content"]

    async def _collect_messages(self, watch_iterator):
        """Helper method to collect messages from watch iterator."""
        messages = []
        try:
            async for message in watch_iterator:
                messages.append(message)
        except asyncio.CancelledError:
            pass
        return messages
