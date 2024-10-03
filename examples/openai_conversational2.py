import os
import logging

from openai import AsyncOpenAI
from pywa import WhatsApp
from pywa.types import Message
import jsonlines
from collections import defaultdict
import json
import instructor
from pydantic import BaseModel, Field
from typing import List, AsyncGenerator
import asyncio
from datetime import datetime, timedelta
from asyncio import Task
from typing import Any

from fastapi import FastAPI
import argparse
import uvicorn


mng = "EAAXiFHiqNhwBOy6fVuQXWyybH237mtkFZCT36NgwdlrYLeauG5qL3EZATg5OVuetBDO5hJlxJBB8wXWDs4QSDEjoNkAmzXjAKuZANj4oZBL8r7gyM0MGgMZCRADlZBsTmUagOwFc1PlEY01ZAZB9k6g7sJqi3E8e1lKohsOv7ByZAdTMMPih5FS8ngJryo9d9vzrS3QXbTZCs5UmSes5odvTRv1ZAxUCKaA9VjzQtJC2YTS"


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

# Initialize the ConversationHistory
conversation_history: defaultdict[str, list[dict[str, str]]] = defaultdict(list)

# Store the last message timestamp for each user
last_message_time: dict[str, datetime] = {}

# Store the tasks for each user
user_tasks: dict[str, Task[Any]] = {}


async def get_openai_response(message: str, phone_number: str) -> str:
    """
    Requests a response from OpenAI based on the input message and conversation history.
    """
    conversation = conversation_history[phone_number]
    messages = (
        [
            {"role": "system", "content": "You are a helpful assistant."},
        ]
        + conversation
        + [
            {"role": "user", "content": message},
        ]
    )

    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        max_tokens=300,  # Increased to allow for longer initial responses
    )
    return response.choices[0].message.content.strip()


async def process_and_respond(client: WhatsApp, msg: Message):
    """
    Process the message and send a response after a delay.
    """
    try:
        if msg.text is None:
            raise ValueError("Message text is None")

        conversation_history[msg.from_user.wa_id].append(
            {"role": "user", "content": msg.text}
        )

        # Wait for 5 seconds before processing
        await asyncio.sleep(5)

        # Check if this task has been cancelled
        if asyncio.current_task().cancelled():
            return

        response = await get_openai_response(msg.text, msg.from_user.wa_id)

        client.send_message(to=msg.from_user.wa_id, text=response)
        conversation_history[msg.from_user.wa_id].append(
            {"role": "assistant", "content": response}
        )
        logging.info(f"SENT,{msg.from_user.wa_id},{response}")

    except ValueError as ve:
        logging.error(f"ValueError: {ve}")
        client.send_message(
            to=msg.from_user.wa_id,
            text="Sorry, I couldn't process your message. Please try again.",
        )
    except asyncio.CancelledError:
        logging.info(f"Task cancelled for user {msg.from_user.wa_id}")
    except Exception as e:
        logging.error(f"Error processing message: {e}")
        client.send_message(
            to=msg.from_user.wa_id,
            text="Sorry, I couldn't generate a response right now. Please try again later.",
        )


@wa.on_message()
async def respond_message(client: WhatsApp, msg: Message):
    """
    Handles all incoming messages, moderates them, and responds with an OpenAI-generated response.
    """
    current_time = datetime.now()
    user_id = msg.from_user.wa_id

    # Cancel any existing task for this user
    if user_id in user_tasks:
        user_tasks[user_id].cancel()

    # Create a new task for this message
    task = asyncio.create_task(process_and_respond(client, msg))
    user_tasks[user_id] = task

    # Update the last message time for this user
    last_message_time[user_id] = current_time


def start_server():
    uvicorn.run(app, host="0.0.0.0", port=8080)


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
