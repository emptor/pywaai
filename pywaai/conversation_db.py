import os
import sqlite3
from cachetools import TTLCache
from sqlcipher3 import dbapi2 as sqlcipher
import asyncio
import logging
from typing import Dict, List, Optional, Any, AsyncIterator
from datetime import datetime
import time
import secrets
from base64 import b64encode, b64decode
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
import json
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import desc, create_engine, QueuePool
from .models import ConversationDB, Base


def generate_ulid() -> str:
    """Generate a ULID (Universally Unique Lexicographically Sortable Identifier).

    Returns:
        A 26-character string containing timestamp and randomness components.
    """
    # Get current timestamp in milliseconds
    timestamp = int(time.time() * 1000)

    # Convert timestamp to base32 (first 10 chars)
    # We use a custom alphabet that's compatible with ULID spec
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    timestamp_str = ""
    for _ in range(10):
        timestamp_str = alphabet[timestamp & 31] + timestamp_str
        timestamp = timestamp >> 5

    # Generate 16 random characters for the randomness component
    randomness = ""
    for _ in range(16):
        randomness += alphabet[secrets.randbelow(32)]

    return timestamp_str + randomness


try:
    from loguru import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


class ConnectionPool:
    """Manages a pool of SQLAlchemy sessions."""

    def __init__(self, db_path: str, pool_size: int = 5):
        """Initialize the connection pool."""
        self.db_path = db_path
        self.pool_size = pool_size
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            poolclass=QueuePool,
            pool_size=pool_size,
            max_overflow=0,
        )
        self.Session = sessionmaker(bind=self.engine)
        self.all_sessions = []
        self._initialized = False
        self._lock = asyncio.Lock()

    async def init_db(self):
        """Initialize the database schema."""
        async with self._lock:
            if not self._initialized:
                Base.metadata.create_all(self.engine)
                self._initialized = True

    async def get_connection(self) -> Session:
        """Get a connection from the pool."""
        if not self._initialized:
            await self.init_db()

        async with self._lock:
            if len(self.all_sessions) < self.pool_size:
                session = self.Session()
                self.all_sessions.append(session)
                return session

            # Try to find a session that's not in use
            for session in self.all_sessions:
                if not session.in_transaction():
                    return session

            # If all sessions are in use, create a new one
            session = self.Session()
            self.all_sessions.append(session)
            return session

    async def release_connection(self, session: Session):
        """Release a connection back to the pool."""
        if session.in_transaction():
            session.rollback()

        async with self._lock:
            if len(self.all_sessions) > self.pool_size:
                session.close()
                self.all_sessions.remove(session)

    async def close_all(self):
        """Close all connections in the pool."""
        async with self._lock:
            for session in self.all_sessions:
                try:
                    if session.in_transaction():
                        session.rollback()
                    session.close()
                except Exception:
                    pass
            self.all_sessions.clear()
            self.engine.dispose()


class ConversationHistory:
    """Manages conversation history with SQLite backend."""

    def __init__(self, db_path: str = "conversations.db", pool_size: int = 5):
        """Initialize the conversation history manager."""
        self.db_path = db_path
        self.pool = ConnectionPool(db_path, pool_size)

    async def init_db(self):
        """Initialize the database."""
        await self.pool.init_db()

    async def append(
        self, phone_number: str, message: Dict[str, Any], conversation_id: str
    ) -> str:
        """Append a message to the conversation history."""
        session = await self.pool.get_connection()
        try:
            conversation = (
                session.query(ConversationDB)
                .filter(
                    ConversationDB.phone_number == phone_number,
                    ConversationDB.conversation_id == conversation_id,
                )
                .first()
            )
            if not conversation:
                raise ValueError(f"Conversation {conversation_id} not found")

            messages = json.loads(conversation.messages)
            messages.append(message)
            conversation.messages = json.dumps(messages)
            conversation.updated_at = datetime.utcnow()
            session.commit()
            return conversation_id
        finally:
            await self.pool.release_connection(session)

    async def read(
        self, phone_number: str, conversation_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Read messages from a conversation."""
        session = await self.pool.get_connection()
        try:
            query = session.query(ConversationDB).filter(
                ConversationDB.phone_number == phone_number
            )
            if conversation_id:
                query = query.filter(ConversationDB.conversation_id == conversation_id)
            conversations = query.all()

            all_messages = []
            for conversation in conversations:
                messages = json.loads(conversation.messages)
                all_messages.extend(messages)
            return all_messages
        finally:
            await self.pool.release_connection(session)

    async def watch(
        self, phone_number: str, conversation_id: str
    ) -> AsyncIterator[Dict[str, Any]]:
        """Watch for changes in a conversation."""
        session = await self.pool.get_connection()
        try:
            conversation = (
                session.query(ConversationDB)
                .filter(
                    ConversationDB.phone_number == phone_number,
                    ConversationDB.conversation_id == conversation_id,
                )
                .first()
            )
            if not conversation:
                raise ValueError(f"Conversation {conversation_id} not found")

            last_check = datetime.utcnow()
            while True:
                session.refresh(conversation)
                if conversation.updated_at > last_check:
                    messages = json.loads(conversation.messages)
                    for message in messages:
                        if (
                            "timestamp" not in message
                            or message["timestamp"] > last_check
                        ):
                            yield message
                    last_check = conversation.updated_at
                await asyncio.sleep(1)
        finally:
            await self.pool.release_connection(session)

    async def __getitem__(self, key: tuple[str, Optional[str]]) -> List[Dict[str, Any]]:
        phone_number, conversation_id = key
        return await self.read(phone_number, conversation_id)

    async def __setitem__(
        self, key: tuple[str, Optional[str]], value: List[Dict[str, Any]]
    ):
        phone_number, conversation_id = key
        session = await self.pool.get_connection()
        try:
            # Delete existing messages for this conversation
            session.query(ConversationDB).filter(
                ConversationDB.phone_number == phone_number,
                ConversationDB.conversation_id == conversation_id,
            ).delete()

            # Insert new messages
            for message in value:
                conversation = ConversationDB(
                    conversation_id=conversation_id,
                    phone_number=phone_number,
                    messages=json.dumps([message]),
                    updated_at=datetime.utcnow(),
                )
                session.add(conversation)
            session.commit()

            # Update cache
            cache_key = (phone_number, conversation_id)
            self.message_cache[cache_key] = value
        finally:
            await self.pool.release_connection(session)


class EncryptedConversationHistory(ConversationHistory):
    """Manages encrypted conversation history."""

    def __init__(
        self,
        db_path: str = "encrypted_conversations.db",
        master_key: Optional[str] = None,
        salt_master_key: Optional[str] = None,
        pool_size: int = 5,
    ):
        """Initialize the encrypted conversation history manager."""
        super().__init__(db_path, pool_size)
        self.cipher = self._init_cipher(master_key, salt_master_key)

    def _init_cipher(
        self, master_key: Optional[str], salt_master_key: Optional[str]
    ) -> AESGCM:
        """Initialize the encryption cipher."""
        if not master_key:
            master_key = os.environ.get("MASTER_KEY")
        if not salt_master_key:
            salt_master_key = os.environ.get("SALT_MASTER_KEY")
        if not master_key or not salt_master_key:
            raise ValueError(
                "Master key and salt master key are required for encryption"
            )

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,  # 256 bits
            salt=salt_master_key.encode(),
            iterations=100000,
        )
        key = kdf.derive(master_key.encode())  # Use raw bytes, not base64 encoded
        return AESGCM(key)

    def _encrypt_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Encrypt a message."""
        nonce = os.urandom(12)
        message_bytes = json.dumps(message).encode()
        encrypted = self.cipher.encrypt(nonce, message_bytes, None)
        return {
            "nonce": b64encode(nonce).decode(),
            "encrypted": b64encode(encrypted).decode(),
        }

    def _decrypt_message(self, encrypted_message: Dict[str, Any]) -> Dict[str, Any]:
        """Decrypt a message."""
        nonce = b64decode(encrypted_message["nonce"])
        encrypted = b64decode(encrypted_message["encrypted"])
        decrypted = self.cipher.decrypt(nonce, encrypted, None)
        return json.loads(decrypted.decode())

    async def append(
        self, phone_number: str, message: Dict[str, Any], conversation_id: str
    ) -> str:
        """Append an encrypted message to the conversation history."""
        encrypted_message = self._encrypt_message(message)
        return await super().append(phone_number, encrypted_message, conversation_id)

    async def watch(
        self, phone_number: str, conversation_id: str
    ) -> AsyncIterator[Dict[str, Any]]:
        """Watch for changes in a conversation and decrypt messages."""
        async for encrypted_message in super().watch(phone_number, conversation_id):
            yield self._decrypt_message(encrypted_message)

    async def read(
        self, phone_number: str, conversation_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        encrypted_messages = await super().read(phone_number, conversation_id)
        decrypted_messages = []
        for encrypted_message in encrypted_messages:
            decrypted_message = self._decrypt_message(encrypted_message)
            decrypted_messages.append(decrypted_message)
        return decrypted_messages


from dataclasses import dataclass
from datetime import datetime


@dataclass
class Conversation:
    """Represents a single conversation."""

    conversation_id: str
    phone_number: str
    messages: List[Dict[str, Any]]
    created_at: datetime
    updated_at: datetime


class ConversationManager:
    """Manages conversations with message history."""

    def __init__(
        self,
        db_path: str = "conversations.db",
        pool_size: int = 5,
        history: Optional[ConversationHistory] = None,
    ):
        """Initialize the conversation manager."""
        self.history = history or ConversationHistory(
            db_path=db_path, pool_size=pool_size
        )

    async def init_db(self):
        """Initialize the database."""
        await self.history.init_db()

    async def create_conversation(self, phone_number: str) -> Conversation:
        """Create a new conversation."""
        conversation_id = generate_ulid()
        now = datetime.utcnow()
        session = await self.history.pool.get_connection()
        try:
            conversation = ConversationDB(
                conversation_id=conversation_id,
                phone_number=phone_number,
                messages=json.dumps([]),
                created_at=now,
                updated_at=now,
            )
            session.add(conversation)
            session.commit()
            return Conversation(
                phone_number=phone_number,
                conversation_id=conversation_id,
                messages=[],
                created_at=now,
                updated_at=now,
            )
        finally:
            await self.history.pool.release_connection(session)

    async def get_conversations(
        self, phone_number: str, limit: Optional[int] = None
    ) -> List[Conversation]:
        """Get all conversations for a phone number."""
        session = await self.history.pool.get_connection()
        try:
            query = (
                session.query(ConversationDB)
                .filter(ConversationDB.phone_number == phone_number)
                .order_by(desc(ConversationDB.updated_at))
            )
            if limit:
                query = query.limit(limit)
            conversations = query.all()
            return [
                Conversation(
                    phone_number=conv.phone_number,
                    conversation_id=conv.conversation_id,
                    messages=json.loads(conv.messages),
                    created_at=conv.created_at,
                    updated_at=conv.updated_at,
                )
                for conv in conversations
            ]
        finally:
            await self.history.pool.release_connection(session)

    async def get_latest_conversation(
        self, phone_number: str
    ) -> Optional[Conversation]:
        """Get the latest conversation for a phone number."""
        session = await self.history.pool.get_connection()
        try:
            latest = (
                session.query(ConversationDB)
                .filter(ConversationDB.phone_number == phone_number)
                .order_by(desc(ConversationDB.updated_at))
                .first()
            )
            if latest:
                return Conversation(
                    phone_number=latest.phone_number,
                    conversation_id=latest.conversation_id,
                    messages=json.loads(latest.messages),
                    created_at=latest.created_at,
                    updated_at=latest.updated_at,
                )
            return None
        finally:
            await self.history.pool.release_connection(session)

    async def add_message(
        self, phone_number: str, message: Dict[str, Any], conversation_id: str
    ) -> str:
        """Add a message to a conversation."""
        return await self.history.append(phone_number, message, conversation_id)

    async def get_messages(
        self, phone_number: str, conversation_id: str
    ) -> List[Dict[str, Any]]:
        """Get all messages in a conversation."""
        return await self.history.read(phone_number, conversation_id)

    async def watch_conversation(
        self, phone_number: str, conversation_id: str
    ) -> AsyncIterator[Dict[str, Any]]:
        """Watch for changes in a conversation."""
        async for message in self.history.watch(phone_number, conversation_id):
            yield message

    async def get_all_phone_numbers(self) -> List[str]:
        """Get all unique phone numbers that have conversations.

        Returns:
            List[str]: A list of unique phone numbers.
        """
        session = await self.history.pool.get_connection()
        try:
            # Query for distinct phone numbers
            phone_numbers = [
                row[0]
                for row in session.query(ConversationDB.phone_number).distinct().all()
            ]
            return phone_numbers
        finally:
            await self.history.pool.release_connection(session)
            
    async def delete_conversation(self, phone_number: str, conversation_id: str) -> bool:
        """Delete a conversation.
        
        Args:
            phone_number: The phone number associated with the conversation.
            conversation_id: The ID of the conversation to delete.
            
        Returns:
            bool: True if the conversation was deleted, False if it didn't exist.
        """
        session = await self.history.pool.get_connection()
        try:
            result = session.query(ConversationDB).filter(
                ConversationDB.phone_number == phone_number,
                ConversationDB.conversation_id == conversation_id
            ).delete()
            session.commit()
            # Return True if at least one row was deleted
            return result > 0
        finally:
            await self.history.pool.release_connection(session)
