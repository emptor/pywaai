import os
import logging
from fastapi import FastAPI
from openai import AsyncOpenAI
from pywa import WhatsApp
from pywa.types import Message
import jsonlines
from collections import defaultdict
import json
import instructor
from pydantic import BaseModel, Field
from typing import List, AsyncGenerator, Dict, Any
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
import base64
import sqlcipher3
import argparse
import asyncio
import uvicorn


app = FastAPI()

phone_id = "392248423969335"
app_id = 1655952435197468
app_secret = "9bfe44b4a12ba3f793282a6136203eea"
verify_token = "ABD361"
callback_url = "https://whatsapp.emptor-cdn.com"
business_account_id = "391057337423244"
verify_timeout = 10

wa = WhatsApp(
    token=mng,
    phone_id=phone_id,
    app_id=app_id,
    app_secret=app_secret,  # Required for validation
    server=app,
    verify_token=verify_token,
    callback_url=callback_url,  # Replace with your public callback URL
    business_account_id=business_account_id,
    verify_timeout=10,
)


openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

import os
import time
from cachetools import TTLCache
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
conversation_history = ConversationHistory()


class ShorterResponses(BaseModel):
    """A rewritten list of messages based on the original response, but more succint, interesting and modular across multiple messages."""

    messages: List[str] = Field(..., description="A list of 2-4 shorter messages")

    def dict(self):
        return {"messages": [message for message in self.messages]}


async def get_shorter_responses(
    response: str, client: WhatsApp, msg: Message
) -> AsyncGenerator[str, None]:
    shortener_prompt = """
    You are an assistant tasked with splitting a long message into 2-4 shorter WhatsApp-friendly messages.
    Each message should be self-contained and coherent.
    Ensure messages are well-formatted and easy to read on mobile devices.
    """
    messages = [
        {"role": "system", "content": shortener_prompt},
        {"role": "user", "content": response},
    ]

    try:

        shortener_client = instructor.patch(openai_client)

        stream = shortener_client.chat.completions.create_partial(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=800,
            response_model=ShorterResponses,
            stream=True,
        )

        async for shorter_response in stream:
            for message in shorter_response.messages:
                logging.info(f"Shorter response: {message}")
                client.send_message(to=msg.from_user.wa_id, text=message)
                await conversation_history.append(
                    msg.from_user.wa_id, {"role": "assistant", "content": message}
                )
                yield message

    except Exception as e:
        logging.error(f"Error in get_shorter_responses: {e}")
        # If there's an error, yield the original response
        client.send_message(to=msg.from_user.wa_id, text=response)
        await conversation_history.append(
            msg.from_user.wa_id, {"role": "assistant", "content": response}
        )
        yield response


async def get_openai_response(message: str, phone_number: str) -> str:
    """
    Requests a response from OpenAI based on the input message and conversation history.
    """
    conversation = await conversation_history[phone_number]
    current_time = datetime.now()
    peru_time = current_time.astimezone(ZoneInfo("America/Lima"))
    formatted_date = peru_time.date().isoformat()
    formatted_time = (
        peru_time.replace(
            minute=0 if peru_time.minute < 30 else 30, second=0, microsecond=0
        )
        .time()
        .isoformat(timespec="minutes")
    )

    system_prompt = f"You are a helpful assistant. Today's date is {formatted_date} and the current time is {formatted_time}. The user's phone number is: {phone_number}"

    messages = (
        [
            {"role": "system", "content": system_prompt},
        ]
        + conversation
        + [
            {"role": "user", "content": message},
        ]
    )

    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        max_tokens=800,
    )

    return (
        response.choices[0].message.content.strip()
        if response.choices[0].message.content
        else "I'm sorry, I couldn't retrieve the requested information."
    )


@wa.on_message()
async def respond_message(client: WhatsApp, msg: Message):
    """
    Handles all incoming messages, moderates them, and responds with an OpenAI-generated response.
    """
    try:
        if msg.text is None:
            raise ValueError("Message text is None")

        # Moderate the incoming message
        moderation = await openai_client.moderations.create(input=msg.text)
        if moderation.results[0].flagged:
            client.send_message(
                to=msg.from_user.wa_id,
                text="I'm sorry, but I can't respond to that kind of message.",
            )
            logging.warning(f"Flagged message from {msg.from_user.wa_id}: {msg.text}")
            return

        await conversation_history.append(
            msg.from_user.wa_id, {"role": "user", "content": msg.text}
        )
        response = await get_openai_response(msg.text, msg.from_user.wa_id)

        if len(response) > 300:
            async for part in get_shorter_responses(response, client, msg):
                logging.info(f"SENT,{msg.from_user.wa_id},{part}")
        else:
            client.send_message(to=msg.from_user.wa_id, text=response)
            await conversation_history.append(
                msg.from_user.wa_id, {"role": "assistant", "content": response}
            )
            logging.info(f"SENT,{msg.from_user.wa_id},{response}")

    except ValueError as ve:
        logging.error(f"ValueError: {ve}")
        client.send_message(
            to=msg.from_user.wa_id,
            text="Sorry, I couldn't process your message. Please try again.",
        )
    except Exception as e:
        logging.error(f"Error processing message: {e}")
        client.send_message(
            to=msg.from_user.wa_id,
            text="Sorry, I couldn't generate a response right now. Please try again later.",
        )


def start_server():
    uvicorn.run(app, host="0.0.0.0", port=8080)


def read_conversation(phone_number: str):
    conversation = asyncio.run(conversation_history[phone_number])
    for message in conversation:
        role = message["role"]
        content = message["content"]
        print(f"{role.capitalize()}: {content}")


async def watch_phone_number(phone_number: str):
    """
    Watches a phone number and prints new messages as they are added to the conversation history.
    """
    last_length = len(await conversation_history[phone_number])
    conn = sqlcipher3.connect(conversation_history.db_path)
    conn.execute("PRAGMA key = '{}';".format(conversation_history.master_key))
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
                    await conversation_history.decrypt(
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WhatsApp Bot")
    parser.add_argument(
        "action", choices=["start", "send", "read", "watch"], help="Action to perform"
    )
    parser.add_argument("--phone", help="Phone number for send, read, or watch action")
    parser.add_argument("--message", help="Message to send (optional)", default="")
    args = parser.parse_args()
    if args.action == "start":
        start_server()
    elif args.action == "send":
        if not args.phone:
            print("Phone number is required for send action")
        else:
            asyncio.run(send_message(args.phone, args.message))
    elif args.action == "read":
        if not args.phone:
            print("Phone number is required for read action")
        else:
            read_conversation(args.phone)
            time.sleep(5)  # Sleep for 5 seconds to ensure the server runs long enough
    elif args.action == "watch":
        if not args.phone:
            print("Phone number is required for watch action")
        else:
            asyncio.run(watch_phone_number(args.phone))
            time.sleep(5)  # Sleep for 5 seconds to ensure the server runs long enough
