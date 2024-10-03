import os
import logging
from fastapi import FastAPI
from openai import AsyncOpenAI
from pywa import WhatsApp
from pywa.types import Message
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Dict, Any
import uvicorn

# TODO: Add stages to the conversation

# Setup logging
from loguru import logger

# Environment variables
mng = os.getenv("WHATSAPP_MANAGER_TOKEN")
openai_api_key = os.environ.get("OPENAI_API_KEY")

app = FastAPI()

# WhatsApp setup
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
    app_secret=app_secret,
    server=app,
    verify_token=verify_token,
    callback_url=callback_url,
    business_account_id=business_account_id,
    verify_timeout=verify_timeout,
)

openai_client = AsyncOpenAI(api_key=openai_api_key)

conversation_history = defaultdict(list)


async def get_openai_response(message: str, phone_number: str) -> str:
    """
    Requests a response from OpenAI based on the input message and conversation history.
    """
    conversation: List[Dict[str, Any]] = conversation_history[phone_number]
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

    system_prompt = (
        f"You are a helpful assistant."
        f"Today's date is {formatted_date} and the current time is {formatted_time}."
        f"The user's phone number is: {phone_number}."
    )

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
        model="gpt-4o-mini",
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

        conversation_history[msg.from_user.wa_id].append(
            {"role": "user", "content": msg.text}
        )
        response = await get_openai_response(msg.text, msg.from_user.wa_id)

        msg.reply_text(text=response)
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
    except Exception as e:
        logging.error(f"Error processing message: {e}")
        client.send_message(
            to=msg.from_user.wa_id,
            text="Sorry, I couldn't generate a response right now. Please try again later.",
        )


# Run the server
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8080)
