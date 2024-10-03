import os
import logging
from fastapi import FastAPI
from openai import AsyncOpenAI
from pywa import WhatsApp
from pywa.types import Message
from datetime import datetime
from zoneinfo import ZoneInfo
import time
import argparse
import asyncio
import uvicorn
from typing import List, Dict

from db_utils import (
    EncryptedConversationHistory,
)
from wa_utils import send_message, generate_response

app = FastAPI()

wa = WhatsApp(
    token=os.getenv("WA_TOKEN", ""),
    phone_id=os.getenv("WA_PHONE_ID", ""),
    app_id=int(os.getenv("WA_APP_ID", "")),
    app_secret=os.getenv("WA_APP_SECRET", ""),
    server=app,
    verify_token=os.getenv("WA_VERIFY_TOKEN", ""),
    callback_url=f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN')}",
    verify_timeout=10,
)

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
conversation_history = EncryptedConversationHistory()


@wa.on_message()
async def respond_message(client: WhatsApp, msg: Message):
    """
    Handles all incoming messages, moderates them, and responds with an OpenAI-generated response.
    """
    # try:
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
    response: List[Dict[str, str]] = await generate_response(
        msg,
        conversation_history,
        msg.from_user.wa_id,
        timezone="America/Lima",
        system_prompt="You are a helpful assistant.",
        model="gpt-4o",
    )

    for part in response:
        client.send_message(to=msg.from_user.wa_id, text=part["content"])
        await conversation_history.append(
            msg.from_user.wa_id, {"role": "assistant", "content": part["content"]}
        )
        logging.info(f"SENT,{msg.from_user.wa_id},{part['content']}")
    """
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
    """


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
            asyncio.run(send_message(wa, args.phone, args.message))
    elif args.action == "read":
        if not args.phone:
            print("Phone number is required for read action")
        else:
            conversation_history.read(args.phone)
            time.sleep(5)  # Sleep for 5 seconds to ensure the server runs long enough
    elif args.action == "watch":
        if not args.phone:
            print("Phone number is required for watch action")
        else:
            asyncio.run(conversation_history.watch(args.phone))
            time.sleep(5)  # Sleep for 5 seconds to ensure the server runs long enough
