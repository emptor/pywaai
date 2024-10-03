import os
import logging
from datetime import datetime, timedelta
import asyncio

from openai import AsyncOpenAI
from pywa import WhatsApp
from pywa.types import Message
from collections import defaultdict
from typing import List, Dict, Any

from fastapi import FastAPI
import argparse
import uvicorn

# Setup logging
from loguru import logger

# Environment variables
mng = os.getenv("WHATSAPP_MANAGER_TOKEN")
openai_api_key = os.environ.get("OPENAI_API_KEY")

app = FastAPI()


wa = WhatsApp(
    token=mng,
    phone_id=phone_id,
    app_id=app_id,
    app_secret=app_secret,
    server=app,
    verify_token=verify_token,
    callback_url=callback_url,
    business_account_id=business_account_id,
    verify_timeout=verify_timeout,
)
openai_client = AsyncOpenAI(api_key=openai_api_key)

# Conversation and user state management
conversation_history: Dict[str, List[Dict[str, str]]] = defaultdict(list)
last_message_time: Dict[str, datetime] = {}
user_tasks: Dict[str, asyncio.Task] = {}

# Follow-up schedule (in minutes)
FOLLOW_UP_SCHEDULE = [1, 2, 3]  # 5 min, 30 min, 4 hours


# Function to generate a new message from OpenAI
async def get_openai_generation(phone_number: str) -> str:
    """
    Generates a new message for the thread based on the conversation history.
    """
    conversation = conversation_history[phone_number]
    messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant. When you follow up, don't be repetitive and try to be interesting. Tell them a joke about computers every couple of follow-ups.",
        },
    ] + conversation

    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        max_tokens=300,
    )
    return response.choices[0].message.content


async def get_openai_response(message: str, phone_number: str) -> str:
    """Request a response from OpenAI based on the conversation history."""
    conversation = conversation_history[phone_number]
    messages = (
        [
            {"role": "system", "content": "You are a helpful assistant."},
        ]
        + conversation
        + [{"role": "user", "content": message}]
    )

    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        max_tokens=300,
    )
    return response.choices[0].message.content


async def send_follow_up(client: WhatsApp, user_id: str, delay: int):
    """Send a follow-up message after the specified delay."""
    await asyncio.sleep(delay * 60)  # Convert minutes to seconds
    if datetime.now() - last_message_time[user_id] >= timedelta(minutes=delay):
        follow_up_message = await get_openai_generation(user_id)
        client.send_message(to=user_id, text=follow_up_message)
        logger.info(f"Sent follow-up to {user_id} after {delay} minutes")
        conversation_history[user_id].append(
            {"role": "assistant", "content": follow_up_message}
        )


async def schedule_follow_ups(client: WhatsApp, user_id: str):
    """Schedule follow-up messages for a user."""
    for delay in FOLLOW_UP_SCHEDULE:
        asyncio.create_task(send_follow_up(client, user_id, delay))


async def process_and_respond(client: WhatsApp, msg: Message):
    """Process the incoming message and send a response."""
    try:
        if msg.text is None:
            raise ValueError("Message text is None")

        user_id = msg.from_user.wa_id
        conversation_history[user_id].append({"role": "user", "content": msg.text})

        response = await get_openai_response(msg.text, user_id)
        client.send_message(to=user_id, text=response)
        conversation_history[user_id].append({"role": "assistant", "content": response})
        logger.info(f"SENT,{user_id},{response}")

        # Update last message time and schedule follow-ups
        last_message_time[user_id] = datetime.now()
        await schedule_follow_ups(client, user_id)

    except Exception as e:
        logger.error(f"Error processing message: {e}")
        client.send_message(
            to=msg.from_user.wa_id,
            text="Sorry, I couldn't generate a response right now. Please try again later.",
        )


@wa.on_message()
async def respond_message(client: WhatsApp, msg: Message):
    """Handle incoming messages and manage user tasks."""
    user_id = msg.from_user.wa_id

    # Cancel any existing follow-up tasks for this user
    if user_id in user_tasks:
        user_tasks[user_id].cancel()

    # Create a new task for processing this message
    task = asyncio.create_task(process_and_respond(client, msg))
    user_tasks[user_id] = task


def start_server():
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="WhatsApp Bot with Follow-up Scheduling"
    )
    parser.add_argument("action", choices=["start"], help="Action to perform")
    args = parser.parse_args()
    if args.action == "start":
        start_server()
