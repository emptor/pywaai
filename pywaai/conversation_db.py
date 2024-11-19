import os
import sqlite3
from cachetools import TTLCache
from sqlcipher3 import dbapi2 as sqlcipher
import asyncio
import logging
from typing import Dict, List, Optional, Any, AsyncIterator
from datetime import datetime
from ulid import ULID
from base64 import b64encode, b64decode
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
import json

try:
    from loguru import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


class ConnectionPool:
    def __init__(self, db_path: str, pool_size: int = 5, encrypted: bool = False):
        self.db_path = db_path
        self.pool_size = pool_size
        self.pool = asyncio.Queue(maxsize=pool_size)
        self.encrypted = encrypted
        self.all_connections = set()
        for _ in range(pool_size):
            if encrypted:
                conn = sqlcipher.connect(db_path)
                conn.execute("PRAGMA key = '{}';".format(self.master_key))
            else:
                conn = sqlite3.connect(db_path, uri=True, check_same_thread=False)
            self.pool.put_nowait(conn)
            self.all_connections.add(conn)
        logger.debug(f"Initialized ConnectionPool with {pool_size} connections")

    async def get_connection(self):
        conn = await self.pool.get()
        logger.debug("Got a connection from the pool")
        return conn

    async def release_connection(self, conn):
        await self.pool.put(conn)
        logger.debug("Released a connection back to the pool")

    async def close_all(self):
        logger.debug("Starting to close all connections")
        close_tasks = []
        # Close all connections in all_connections
        for conn in self.all_connections:
            close_tasks.append(asyncio.create_task(self._close_connection(conn)))
        if close_tasks:
            await asyncio.gather(*close_tasks)
        logger.debug(
            f"Finished closing all connections. Closed {len(close_tasks)} connections."
        )

    async def _close_connection(self, conn):
        try:
            conn.close()
            logger.debug("Closed a connection")
        except Exception as e:
            logger.error(f"Error closing connection: {e}")


class ConversationHistory:
    def __init__(
        self,
        db_path: str = "conversations.db",
        cache_ttl: int = 86400,  # 1 day in seconds
        cache_maxsize: int = 1000,  # Maximum number of items in each cache
        pool_size: int = 5,  # Connection pool size
    ):
        self.db_path = db_path
        self.message_cache = TTLCache(maxsize=cache_maxsize, ttl=cache_ttl)
        self.pool = ConnectionPool(db_path, pool_size)

    async def init_db(self):
        conn = await self.pool.get_connection()
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS conversations
                             (phone_number TEXT, conversation_id TEXT, message TEXT, 
                              timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                              PRIMARY KEY (phone_number, conversation_id, timestamp))"""
            )
            conn.commit()
        finally:
            await self.pool.release_connection(conn)

    async def __getitem__(self, key: tuple[str, Optional[str]]) -> List[Dict[str, Any]]:
        phone_number, conversation_id = key
        return await self.read(phone_number, conversation_id)

    async def __setitem__(
        self, key: tuple[str, Optional[str]], value: List[Dict[str, Any]]
    ):
        phone_number, conversation_id = key
        conn = await self.pool.get_connection()
        try:
            # Delete existing messages for this conversation
            conn.execute(
                "DELETE FROM conversations WHERE phone_number = ? AND conversation_id = ?",
                (phone_number, conversation_id),
            )
            # Insert new messages
            for message in value:
                conn.execute(
                    """INSERT INTO conversations 
                       (phone_number, conversation_id, message, timestamp) 
                       VALUES (?, ?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now'))""",
                    (phone_number, conversation_id, json.dumps(message)),
                )
            conn.commit()
            # Update cache
            cache_key = (phone_number, conversation_id)
            self.message_cache[cache_key] = value
        finally:
            await self.pool.release_connection(conn)

    async def get_latest_conversation_id(self, phone_number: str) -> Optional[str]:
        """Get the most recent conversation ID for a phone number."""
        conn = await self.pool.get_connection()
        try:
            cursor = conn.execute(
                """SELECT conversation_id FROM conversations 
                   WHERE phone_number = ? 
                   ORDER BY timestamp DESC LIMIT 1""",
                (phone_number,),
            )
            result = cursor.fetchone()
            return result[0] if result else None
        finally:
            await self.pool.release_connection(conn)

    async def append(
        self,
        phone_number: str,
        message: Dict[str, Any],
        conversation_id: Optional[str] = None,
    ):
        if conversation_id is None:
            # Try to get the latest conversation ID
            latest_id = await self.get_latest_conversation_id(phone_number)
            if latest_id:
                conversation_id = latest_id
            else:
                # If no conversation exists, create a new one
                conversation_id = str(ULID())
        else:
            # Ensure conversation_id is a string
            conversation_id = str(conversation_id)

        conn = await self.pool.get_connection()
        try:
            # Get current microsecond timestamp
            cursor = conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')")
            timestamp = cursor.fetchone()[0]

            # Try to insert with the timestamp
            for attempt in range(3):  # Try up to 3 times with different timestamps
                try:
                    conn.execute(
                        """INSERT INTO conversations 
                           (phone_number, conversation_id, message, timestamp) 
                           VALUES (?, ?, ?, ?)""",
                        (phone_number, conversation_id, json.dumps(message), timestamp),
                    )
                    conn.commit()
                    break
                except sqlite3.IntegrityError:
                    # If we hit a duplicate timestamp, add a small increment
                    timestamp = timestamp[:-1] + str(int(timestamp[-1]) + attempt + 1)
            else:
                raise sqlite3.IntegrityError("Failed to insert after multiple attempts")

            # Update cache if it exists
            cache_key = (phone_number, conversation_id)
            if cache_key in self.message_cache:
                self.message_cache[cache_key].append(message)
        finally:
            await self.pool.release_connection(conn)
        return conversation_id

    async def read(
        self, phone_number: str, conversation_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        cache_key = (phone_number, conversation_id)
        if cache_key in self.message_cache:
            return self.message_cache[cache_key]

        conn = await self.pool.get_connection()
        try:
            if conversation_id is None:
                # Get the most recent conversation if no ID is provided
                cursor = conn.execute(
                    """SELECT conversation_id FROM conversations 
                       WHERE phone_number = ? 
                       ORDER BY timestamp DESC LIMIT 1""",
                    (phone_number,),
                )
                result = cursor.fetchone()
                if result:
                    conversation_id = result[0]
                else:
                    return []

            cursor = conn.execute(
                """SELECT message FROM conversations 
                   WHERE phone_number = ? AND conversation_id = ? 
                   ORDER BY timestamp""",
                (phone_number, conversation_id),
            )
            messages = [json.loads(row[0]) for row in cursor.fetchall()]
            self.message_cache[cache_key] = messages
            return messages
        finally:
            await self.pool.release_connection(conn)

    async def watch(self, phone_number: str, conversation_id: Optional[str] = None):
        """Watch for changes in the conversation."""
        while True:
            messages = await self.read(phone_number, conversation_id)
            yield messages
            await asyncio.sleep(1)  # Prevent tight loop


class EncryptedConversationHistory(ConversationHistory):
    def __init__(
        self,
        db_path: str = "encrypted_conversations.db",
        salt_db_path: str = "encrypted_salts.db",
        master_key: str | None = None,
        salt_master_key: str | None = None,
        cache_ttl: int = 86400,  # 1 day in seconds
        cache_maxsize: int = 1000,  # Maximum number of items in each cache
        pool_size: int = 5,  # Connection pool size
    ):
        if master_key is None and os.environ.get("CONVERSATION_MASTER_KEY") is None:
            raise ValueError(
                "Master key must be provided or set in CONVERSATION_MASTER_KEY environment variable"
            )
        if salt_master_key is None and os.environ.get("SALT_MASTER_KEY") is None:
            raise ValueError(
                "Salt master key must be provided or set in SALT_MASTER_KEY environment variable"
            )
        self.master_key = master_key or os.environ["CONVERSATION_MASTER_KEY"]
        self.salt_master_key = salt_master_key or os.environ["SALT_MASTER_KEY"]
        self.salt_cache = TTLCache(maxsize=cache_maxsize, ttl=cache_ttl)
        self.key_cache = TTLCache(maxsize=cache_maxsize, ttl=cache_ttl)
        self.pool = ConnectionPool(db_path, pool_size, encrypted=True)
        self.salt_pool = ConnectionPool(salt_db_path, pool_size, encrypted=True)
        super().__init__(db_path, cache_ttl, cache_maxsize, pool_size)

    async def init_db(self):
        conn = await self.pool.get_connection()
        salt_conn = await self.salt_pool.get_connection()
        try:
            conn.execute("PRAGMA key = '{}';".format(self.master_key))
            salt_conn.execute("PRAGMA key = '{}';".format(self.salt_master_key))
            conn.execute("PRAGMA cipher_page_size = 4096;")
            conn.execute("PRAGMA kdf_iter = 64000;")
            conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1;")
            conn.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA1;")
            conn.execute("PRAGMA journal_mode=WAL;")
            salt_conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS conversations
                             (phone_number TEXT, conversation_id TEXT, encrypted_message TEXT, nonce TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                              PRIMARY KEY (phone_number, conversation_id, timestamp))"""
            )
            salt_conn.execute(
                """CREATE TABLE IF NOT EXISTS salts
                             (phone_number TEXT PRIMARY KEY, salt TEXT)"""
            )
            conn.commit()
            salt_conn.commit()
        finally:
            await self.pool.release_connection(conn)
            await self.salt_pool.release_connection(salt_conn)

    async def get_or_create_salt(self, phone_number: str) -> bytes:
        if phone_number not in self.salt_cache:
            self.salt_cache[phone_number] = await self._fetch_or_create_salt(
                phone_number
            )
        return self.salt_cache[phone_number]

    async def _fetch_or_create_salt(self, phone_number: str) -> bytes:
        salt_conn = await self.salt_pool.get_connection()
        try:
            salt_conn.execute("PRAGMA key = '{}';".format(self.salt_master_key))
            cursor = salt_conn.cursor()
            cursor.execute(
                "SELECT salt FROM salts WHERE phone_number=?", (phone_number,)
            )
            result = cursor.fetchone()
            if result:
                salt = b64decode(result[0])
            else:
                salt = os.urandom(16)
                cursor.execute(
                    "INSERT INTO salts (phone_number, salt) VALUES (?, ?)",
                    (phone_number, b64encode(salt).decode()),
                )
                salt_conn.commit()
        finally:
            await self.salt_pool.release_connection(salt_conn)
        return salt

    async def derive_key(self, phone_number: str) -> bytes:
        if phone_number not in self.key_cache:
            self.key_cache[phone_number] = await self._derive_key(phone_number)
        return self.key_cache[phone_number]

    async def _derive_key(self, phone_number: str) -> bytes:
        salt = await self.get_or_create_salt(phone_number)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
        )
        return kdf.derive(self.master_key.encode())

    async def encrypt(self, phone_number: str, data: str) -> tuple[bytes, bytes]:
        key = await self.derive_key(phone_number)
        aesgcm = AESGCM(key)
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, data.encode(), None)
        return ciphertext, nonce

    async def decrypt(self, phone_number: str, ciphertext: bytes, nonce: bytes) -> str:
        key = await self.derive_key(phone_number)
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode()

    async def get_latest_conversation_id(self, phone_number: str) -> Optional[str]:
        """Get the most recent conversation ID for a phone number."""
        conn = await self.pool.get_connection()
        try:
            conn.execute("PRAGMA key = '{}';".format(self.master_key))
            conn.execute("PRAGMA journal_mode=WAL;")
            cursor = conn.execute(
                """SELECT conversation_id FROM conversations 
                   WHERE phone_number = ? 
                   ORDER BY timestamp DESC LIMIT 1""",
                (phone_number,),
            )
            result = cursor.fetchone()
            return result[0] if result else None
        finally:
            await self.pool.release_connection(conn)

    async def __getitem__(self, key: tuple[str, Optional[str]]) -> List[Dict[str, Any]]:
        phone_number, conversation_id = key
        return await self.read(phone_number, conversation_id)

    async def __setitem__(
        self, key: tuple[str, Optional[str]], value: List[Dict[str, Any]]
    ):
        phone_number, conversation_id = key
        conn = await self.pool.get_connection()
        try:
            conn.execute("PRAGMA key = '{}';".format(self.master_key))
            conn.execute("PRAGMA journal_mode=WAL;")
            # Delete existing messages for this conversation
            conn.execute(
                "DELETE FROM conversations WHERE phone_number = ? AND conversation_id = ?",
                (phone_number, conversation_id),
            )
            # Insert new messages
            for message in value:
                encrypted_message, nonce = await self.encrypt(
                    phone_number, json.dumps(message)
                )
                conn.execute(
                    """INSERT INTO conversations 
                       (phone_number, conversation_id, encrypted_message, nonce, timestamp) 
                       VALUES (?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now'))""",
                    (
                        phone_number,
                        conversation_id,
                        b64encode(encrypted_message).decode(),
                        b64encode(nonce).decode(),
                    ),
                )
            conn.commit()
            # Update cache
            cache_key = (phone_number, conversation_id)
            self.message_cache[cache_key] = value
        finally:
            await self.pool.release_connection(conn)

    async def append(
        self,
        phone_number: str,
        message: Dict[str, Any],
        conversation_id: Optional[str] = None,
    ):
        if conversation_id is None:
            # Try to get the latest conversation ID
            latest_id = await self.get_latest_conversation_id(phone_number)
            if latest_id:
                conversation_id = latest_id
            else:
                # If no conversation exists, create a new one
                conversation_id = str(ULID())
        else:
            # Ensure conversation_id is a string
            conversation_id = str(conversation_id)

        conn = await self.pool.get_connection()
        try:
            # Get current microsecond timestamp
            cursor = conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')")
            timestamp = cursor.fetchone()[0]

            # Try to insert with the timestamp
            for attempt in range(3):  # Try up to 3 times with different timestamps
                try:
                    encrypted_message, nonce = await self.encrypt(
                        phone_number, json.dumps(message)
                    )
                    conn.execute(
                        """INSERT INTO conversations 
                           (phone_number, conversation_id, encrypted_message, nonce, timestamp) 
                           VALUES (?, ?, ?, ?, ?)""",
                        (
                            phone_number,
                            conversation_id,
                            b64encode(encrypted_message).decode(),
                            b64encode(nonce).decode(),
                            timestamp,
                        ),
                    )
                    conn.commit()
                    break
                except sqlite3.IntegrityError:
                    # If we hit a duplicate timestamp, add a small increment
                    timestamp = timestamp[:-1] + str(int(timestamp[-1]) + attempt + 1)
            else:
                raise sqlite3.IntegrityError("Failed to insert after multiple attempts")

            # Update cache if it exists
            cache_key = (phone_number, conversation_id)
            if cache_key in self.message_cache:
                self.message_cache[cache_key].append(message)
        finally:
            await self.pool.release_connection(conn)
        return conversation_id

    async def read(
        self, phone_number: str, conversation_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        cache_key = (phone_number, conversation_id)
        if cache_key in self.message_cache:
            return self.message_cache[cache_key]

        conn = await self.pool.get_connection()
        try:
            conn.execute("PRAGMA key = '{}';".format(self.master_key))
            conn.execute("PRAGMA journal_mode=WAL;")
            if conversation_id is None:
                # Get the most recent conversation if no ID is provided
                cursor = conn.execute(
                    """SELECT conversation_id FROM conversations 
                       WHERE phone_number = ? 
                       ORDER BY timestamp DESC LIMIT 1""",
                    (phone_number,),
                )
                result = cursor.fetchone()
                if result:
                    conversation_id = result[0]
                else:
                    return []

            cursor = conn.execute(
                """SELECT encrypted_message, nonce FROM conversations 
                   WHERE phone_number = ? AND conversation_id = ? 
                   ORDER BY timestamp""",
                (phone_number, conversation_id),
            )
            messages = [
                json.loads(
                    await self.decrypt(
                        phone_number,
                        b64decode(row[0]),
                        b64decode(row[1]),
                    )
                )
                for row in cursor.fetchall()
            ]
            self.message_cache[cache_key] = messages
            return messages
        finally:
            await self.pool.release_connection(conn)

    async def watch(self, phone_number: str, conversation_id: Optional[str] = None):
        """Watch for changes in the conversation."""
        while True:
            messages = await self.read(phone_number, conversation_id)
            yield messages
            await asyncio.sleep(1)  # Prevent tight loop


@dataclass
class Conversation:
    """Represents a single conversation."""

    conversation_id: str
    phone_number: str
    created_at: datetime
    last_message_at: datetime
    message_count: int


class ConversationManager:
    """Manages conversations and provides a standardized interface for both encrypted and unencrypted conversations."""

    def __init__(
        self,
        db_path: str = "conversations.db",
        encrypted: bool = False,
        master_key: Optional[str] = None,
        salt_master_key: Optional[str] = None,
        cache_ttl: int = 86400,
        cache_maxsize: int = 1000,
        pool_size: int = 5,
    ):
        """Initialize the conversation manager."""
        self.encrypted = encrypted
        if encrypted:
            self.history = EncryptedConversationHistory(
                db_path=db_path,
                master_key=master_key,
                salt_master_key=salt_master_key,
                cache_ttl=cache_ttl,
                cache_maxsize=cache_maxsize,
                pool_size=pool_size,
            )
        else:
            self.history = ConversationHistory(
                db_path=db_path,
                cache_ttl=cache_ttl,
                cache_maxsize=cache_maxsize,
                pool_size=pool_size,
            )

    async def init_db(self):
        """Initialize the database."""
        await self.history.init_db()

    async def create_conversation(self, phone_number: str) -> Conversation:
        """Create a new conversation for a phone number."""
        conversation_id = str(ULID())
        # Create an empty message to initialize the conversation
        await self.history.append(
            phone_number=phone_number,
            message={"type": "system", "content": "Conversation started"},
            conversation_id=conversation_id,
        )
        # Get the actual conversation data from the database
        return await self.get_latest_conversation(phone_number)

    async def get_conversations(self, phone_number: str) -> List[Conversation]:
        """Get all conversations for a phone number."""
        conn = await self.history.pool.get_connection()
        try:
            cursor = conn.execute(
                """SELECT DISTINCT
                       conversation_id,
                       MIN(timestamp) as created_at,
                       MAX(timestamp) as last_message_at
                   FROM conversations 
                   WHERE phone_number = ? 
                   GROUP BY conversation_id 
                   ORDER BY last_message_at DESC""",
                (phone_number,),
            )
            conversations = []
            for row in cursor.fetchall():
                conversation_id = row[0]
                message_count = await self.get_message_count(
                    phone_number, conversation_id
                )
                conversations.append(
                    Conversation(
                        conversation_id=conversation_id,
                        phone_number=phone_number,
                        created_at=datetime.fromisoformat(row[1]),
                        last_message_at=datetime.fromisoformat(row[2]),
                        message_count=message_count,
                    )
                )
            return conversations
        finally:
            await self.history.pool.release_connection(conn)

    async def get_latest_conversation(
        self, phone_number: str
    ) -> Optional[Conversation]:
        """Get the latest conversation for a phone number."""
        conn = await self.history.pool.get_connection()
        try:
            cursor = conn.execute(
                """SELECT 
                       conversation_id,
                       MIN(timestamp) as created_at,
                       MAX(timestamp) as last_message_at
                   FROM conversations 
                   WHERE phone_number = ? 
                   GROUP BY conversation_id 
                   ORDER BY last_message_at DESC 
                   LIMIT 1""",
                (phone_number,),
            )
            row = cursor.fetchone()
            if row:
                conversation_id = row[0]
                message_count = await self.get_message_count(
                    phone_number, conversation_id
                )
                return Conversation(
                    conversation_id=conversation_id,
                    phone_number=phone_number,
                    created_at=datetime.fromisoformat(row[1]),
                    last_message_at=datetime.fromisoformat(row[2]),
                    message_count=message_count,
                )
            return None
        finally:
            await self.history.pool.release_connection(conn)

    async def get_messages(
        self,
        phone_number: str,
        conversation_id: Optional[str] = None,
        exclude_system: bool = True,
    ) -> List[Dict[str, Any]]:
        """Get messages for a specific conversation."""
        conn = await self.history.pool.get_connection()
        try:
            if conversation_id:
                if exclude_system:
                    cursor = conn.execute(
                        """SELECT message FROM conversations 
                           WHERE phone_number = ? 
                           AND conversation_id = ?
                           AND json_extract(message, '$.type') IS NULL
                           ORDER BY timestamp ASC""",
                        (phone_number, str(conversation_id)),
                    )
                else:
                    cursor = conn.execute(
                        """SELECT message FROM conversations 
                           WHERE phone_number = ? 
                           AND conversation_id = ?
                           ORDER BY timestamp ASC""",
                        (phone_number, str(conversation_id)),
                    )
            else:
                if exclude_system:
                    cursor = conn.execute(
                        """SELECT message FROM conversations 
                           WHERE phone_number = ? 
                           AND json_extract(message, '$.type') IS NULL
                           ORDER BY timestamp ASC""",
                        (phone_number,),
                    )
                else:
                    cursor = conn.execute(
                        """SELECT message FROM conversations 
                           WHERE phone_number = ? 
                           ORDER BY timestamp ASC""",
                        (phone_number,),
                    )
            return [json.loads(row[0]) for row in cursor.fetchall()]
        finally:
            await self.history.pool.release_connection(conn)

    async def get_message_count(
        self, phone_number: str, conversation_id: str, exclude_system: bool = True
    ) -> int:
        """Get the number of messages in a conversation."""
        conn = await self.history.pool.get_connection()
        try:
            if exclude_system:
                cursor = conn.execute(
                    """SELECT COUNT(*) FROM conversations 
                       WHERE phone_number = ? 
                       AND conversation_id = ?
                       AND json_extract(message, '$.type') IS NULL""",
                    (phone_number, str(conversation_id)),
                )
            else:
                cursor = conn.execute(
                    """SELECT COUNT(*) FROM conversations 
                       WHERE phone_number = ? AND conversation_id = ?""",
                    (phone_number, str(conversation_id)),
                )
            return cursor.fetchone()[0]
        finally:
            await self.history.pool.release_connection(conn)

    async def add_message(
        self,
        phone_number: str,
        message: Dict[str, Any],
        conversation_id: Optional[str] = None,
        create_if_missing: bool = True,
    ) -> str:
        """Add a message to a conversation."""
        if conversation_id is None:
            latest = await self.get_latest_conversation(phone_number)
            if latest:
                conversation_id = latest.conversation_id
            elif create_if_missing:
                conversation_id = await self.create_conversation(phone_number)
            else:
                raise ValueError(
                    "No existing conversation found and create_if_missing is False"
                )

        await self.history.append(phone_number, message, conversation_id)
        return conversation_id

    async def watch_conversation(self, phone_number: str, conversation_id: str):
        """Watch for changes in a conversation."""
        last_timestamp = None
        skip_system_message = True
        while True:
            conn = await self.history.pool.get_connection()
            try:
                if last_timestamp:
                    cursor = conn.execute(
                        """SELECT message, timestamp
                           FROM conversations
                           WHERE phone_number = ?
                           AND conversation_id = ?
                           AND timestamp > ?
                           ORDER BY timestamp ASC""",
                        (phone_number, str(conversation_id), last_timestamp),
                    )
                else:
                    cursor = conn.execute(
                        """SELECT message, timestamp
                           FROM conversations
                           WHERE phone_number = ?
                           AND conversation_id = ?
                           ORDER BY timestamp ASC""",
                        (phone_number, str(conversation_id)),
                    )

                rows = cursor.fetchall()
                for row in rows:
                    message = json.loads(row[0])
                    last_timestamp = row[1]
                    # Skip the initial system message
                    if skip_system_message and message.get("type") == "system":
                        skip_system_message = False
                        continue
                    yield message

                if not rows:
                    await asyncio.sleep(0.1)  # Avoid busy waiting
            finally:
                await self.history.pool.release_connection(conn)
