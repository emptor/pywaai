import os
import sqlite3
from cachetools import TTLCache
from sqlcipher3 import dbapi2 as sqlcipher
import asyncio
import base64
from typing import List, Dict, Any
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
import json
import logging

logger = logging.getLogger(__name__)

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
                conn = sqlite3.connect(db_path, check_same_thread=False)
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
                             (phone_number TEXT, message TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"""
            )
            conn.commit()
        finally:
            await self.pool.release_connection(conn)

    async def __getitem__(self, phone_number: str) -> List[Dict[str, Any]]:
        if phone_number not in self.message_cache:
            conn = await self.pool.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT message FROM conversations WHERE phone_number=? ORDER BY timestamp",
                    (phone_number,),
                )
                messages = [json.loads(row[0]) for row in cursor.fetchall()]
            finally:
                await self.pool.release_connection(conn)
            self.message_cache[phone_number] = messages
        return self.message_cache[phone_number]

    async def __setitem__(self, phone_number: str, value: List[Dict[str, Any]]):
        conn = await self.pool.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM conversations WHERE phone_number=?", (phone_number,)
            )
            for message in value:
                cursor.execute(
                    "INSERT INTO conversations (phone_number, message) VALUES (?, ?)",
                    (phone_number, json.dumps(message)),
                )
            conn.commit()
        finally:
            await self.pool.release_connection(conn)
        self.message_cache[phone_number] = value

    async def append(self, phone_number: str, message: Dict[str, Any]):
        conn = await self.pool.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO conversations (phone_number, message) VALUES (?, ?)",
                (phone_number, json.dumps(message)),
            )
            conn.commit()
        finally:
            await self.pool.release_connection(conn)
        if phone_number in self.message_cache:
            self.message_cache[phone_number].append(message)
        else:
            self.message_cache[phone_number] = await self.__getitem__(phone_number)

    async def read(self, phone_number: str):
        conversation = await self[phone_number]
        for message in conversation:
            role = message["role"]
            content = message["content"]
            print(f"{role.capitalize()}: {content}")

    async def watch(self, phone_number: str):
        last_length = len(await self[phone_number])
        while True:
            current_conversation = await self[phone_number]
            current_length = len(current_conversation)
            if current_length > last_length:
                new_messages = current_conversation[last_length:]
                for message in new_messages:
                    role = message["role"]
                    content = message["content"]
                    print(f"{role.upper()}: {content}")
                last_length = current_length
            await asyncio.sleep(1)


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
                             (phone_number TEXT, encrypted_message TEXT, nonce TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"""
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
                salt = base64.b64decode(result[0])
            else:
                salt = os.urandom(16)
                cursor.execute(
                    "INSERT INTO salts (phone_number, salt) VALUES (?, ?)",
                    (phone_number, base64.b64encode(salt).decode()),
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

    async def __getitem__(self, phone_number: str) -> List[Dict[str, Any]]:
        if phone_number not in self.message_cache:
            conn = await self.pool.get_connection()
            try:
                conn.execute("PRAGMA key = '{}';".format(self.master_key))
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT encrypted_message, nonce FROM conversations WHERE phone_number=? ORDER BY timestamp",
                    (phone_number,),
                )
                messages = [
                    json.loads(
                        await self.decrypt(
                            phone_number,
                            base64.b64decode(row[0]),
                            base64.b64decode(row[1]),
                        )
                    )
                    for row in cursor.fetchall()
                ]
            finally:
                await self.pool.release_connection(conn)
            self.message_cache[phone_number] = messages
        return self.message_cache[phone_number]

    async def __setitem__(self, phone_number: str, value: List[Dict[str, Any]]):
        conn = await self.pool.get_connection()
        try:
            conn.execute("PRAGMA key = '{}';".format(self.master_key))
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM conversations WHERE phone_number=?", (phone_number,)
            )
            for message in value:
                encrypted_message, nonce = await self.encrypt(
                    phone_number, json.dumps(message)
                )
                cursor.execute(
                    "INSERT INTO conversations (phone_number, encrypted_message, nonce) VALUES (?, ?, ?)",
                    (
                        phone_number,
                        base64.b64encode(encrypted_message).decode(),
                        base64.b64encode(nonce).decode(),
                    ),
                )
            conn.commit()
        finally:
            await self.pool.release_connection(conn)
        self.message_cache[phone_number] = value

    async def append(self, phone_number: str, message: Dict[str, Any]):
        conn = await self.pool.get_connection()
        try:
            conn.execute("PRAGMA key = '{}';".format(self.master_key))
            cursor = conn.cursor()
            encrypted_message, nonce = await self.encrypt(
                phone_number, json.dumps(message)
            )
            cursor.execute(
                "INSERT INTO conversations (phone_number, encrypted_message, nonce) VALUES (?, ?, ?)",
                (
                    phone_number,
                    base64.b64encode(encrypted_message).decode(),
                    base64.b64encode(nonce).decode(),
                ),
            )
            conn.commit()
        finally:
            await self.pool.release_connection(conn)
        if phone_number in self.message_cache:
            self.message_cache[phone_number].append(message)
        else:
            self.message_cache[phone_number] = await self.__getitem__(phone_number)

    async def read(self, phone_number: str):
        conversation = await self[phone_number]
        for message in conversation:
            role = message["role"]
            content = message["content"]
            print(f"{role.capitalize()}: {content}")

    async def watch(self, phone_number: str):
        last_length = len(await self[phone_number])
        conn = await self.pool.get_connection()
        try:
            conn.execute("PRAGMA key = '{}';".format(self.master_key))
            conn.execute("PRAGMA journal_mode=WAL;")
            cursor = conn.cursor()
            while True:
                cursor.execute(
                    "SELECT encrypted_message, nonce FROM conversations WHERE phone_number=? ORDER BY timestamp",
                    (phone_number,),
                )
                rows = cursor.fetchall()
                current_length = len(rows)
                if current_length > last_length:
                    new_messages = [
                        json.loads(
                            await self.decrypt(
                                phone_number,
                                base64.b64decode(row[0]),
                                base64.b64decode(row[1]),
                            )
                        )
                        for row in rows[last_length:]
                    ]
                    for message in new_messages:
                        role = message["role"]
                        content = message["content"]
                        print(f"{role.upper()}: {content}")
                    last_length = current_length
                await asyncio.sleep(1)
        finally:
            await self.pool.release_connection(conn)
