import time
import asyncio
import random
import string

import os
from cachetools import TTLCache
from typing import List, Dict, Any
import sqlcipher3
import base64
import json
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes


from sqlcipher3 import dbapi2 as sqlcipher


class ConnectionPool:
    def __init__(self, db_path: str, pool_size: int = 5):
        self.db_path = db_path
        self.pool_size = pool_size
        self.pool = asyncio.Queue(maxsize=pool_size)
        for _ in range(pool_size):
            conn = sqlcipher.connect(db_path)
            self.pool.put_nowait(conn)

    async def get_connection(self):
        return await self.pool.get()

    async def release_connection(self, conn):
        await self.pool.put(conn)

    async def close_all(self):
        while not self.pool.empty():
            conn = await self.pool.get()
            conn.close()


class ConversationHistory:
    def __init__(
        self,
        db_path: str = "conversations.db",
        master_key: str | None = None,
        cache_ttl: int = 86400,  # 1 day in seconds
        cache_maxsize: int = 1000,  # Maximum number of items in each cache
        pool_size: int = 5,  # Connection pool size
    ):
        self.db_path = db_path
        if master_key is None and os.environ.get("CONVERSATION_MASTER_KEY") is None:
            raise ValueError(
                "Master key must be provided or set in CONVERSATION_MASTER_KEY environment variable"
            )
        self.master_key = master_key or os.environ["CONVERSATION_MASTER_KEY"]
        self.salt_cache = TTLCache(maxsize=cache_maxsize, ttl=cache_ttl)
        self.key_cache = TTLCache(maxsize=cache_maxsize, ttl=cache_ttl)
        self.message_cache = TTLCache(maxsize=cache_maxsize, ttl=cache_ttl)
        self.pool = ConnectionPool(db_path, pool_size)

    async def init_db(self):
        conn = await self.pool.get_connection()
        try:
            conn.execute("PRAGMA key = '{}';".format(self.master_key))
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS conversations
                             (phone_number TEXT, encrypted_message TEXT, nonce TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS salts
                             (phone_number TEXT PRIMARY KEY, salt TEXT)"""
            )
        finally:
            await self.pool.release_connection(conn)

    async def get_or_create_salt(self, phone_number: str) -> bytes:
        if phone_number not in self.salt_cache:
            self.salt_cache[phone_number] = await self._fetch_or_create_salt(
                phone_number
            )
        return self.salt_cache[phone_number]

    async def _fetch_or_create_salt(self, phone_number: str) -> bytes:
        conn = await self.pool.get_connection()
        try:
            conn.execute("PRAGMA key = '{}';".format(self.master_key))
            cursor = conn.cursor()
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
                conn.commit()
        finally:
            await self.pool.release_connection(conn)
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


# Initialize the ConversationHistory


def generate_random_string(length):
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def generate_random_message():
    return {
        "role": random.choice(["user", "assistant"]),
        "content": generate_random_string(50),
    }


async def benchmark_operations(conversation_history, num_operations):
    write_tasks = []
    read_tasks = []
    for _ in range(num_operations):
        operation = random.choice(["write", "read"])
        if operation == "write":
            phone_number = generate_random_string(10)
            message = generate_random_message()
            write_tasks.append(
                asyncio.create_task(
                    async_append(conversation_history, phone_number, message)
                )
            )
        elif operation == "read":
            phone_number = generate_random_string(10)
            read_tasks.append(
                asyncio.create_task(async_getitem(conversation_history, phone_number))
            )

    start_time = time.time()
    await asyncio.gather(*write_tasks)
    write_end_time = time.time()
    await asyncio.gather(*read_tasks)
    read_end_time = time.time()

    writes_per_sec = len(write_tasks) / (write_end_time - start_time)
    reads_per_sec = len(read_tasks) / (read_end_time - write_end_time)

    return writes_per_sec, reads_per_sec


async def async_append(conversation_history, phone_number, message):
    await conversation_history.append(phone_number, message)


async def async_getitem(conversation_history, phone_number):
    await conversation_history.__getitem__(phone_number)


async def run_benchmarks():
    conversation_history = ConversationHistory(
        db_path="benchmark.db", master_key="test_key"
    )

    print("Operations Benchmark:")
    for num_operations in [100, 1000, 10000]:
        writes_per_sec, reads_per_sec = await benchmark_operations(
            conversation_history, num_operations
        )
        print(
            f"{num_operations} operations: {writes_per_sec:.2f} writes/sec, {reads_per_sec:.2f} reads/sec"
        )


if __name__ == "__main__":
    asyncio.run(run_benchmarks())
