from openai import OpenAI
import instructor
from pydantic import BaseModel, Field
from typing import List
import os
from pywa import WhatsApp
from pywa.types import Message
from typing import List, AsyncGenerator, Dict, Any
from db_utils import ConversationHistory
from datetime import datetime
from zoneinfo import ZoneInfo
from openai import AsyncOpenAI


try:
    from loguru import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

shortener_tokens_used = 0


def update_token_count(response):
    global shortener_tokens_used
    shortener_tokens_used += response.usage.total_tokens
    logger.info(f"Shortener tokens used in this call: {response.usage.total_tokens}")
    logger.info(f"Total shortener tokens used in this session: {shortener_tokens_used}")


class ShorterResponses(BaseModel):
    """A rewritten list of messages based on the original response, but more succint, interesting and modular across multiple messages."""

    messages: List[str] = Field(..., description="A list of 2-4 shorter messages")

    def dict(self):
        return {"messages": [message for message in self.messages]}


async def get_shorter_responses(response: str) -> ShorterResponses:
    shortener_prompt = """
    Eres un asistente encargado de dividir un mensaje largo en 2-4 mensajes más cortos adecuados para WhatsApp.
    Cada mensaje debe ser completo y tener sentido por sí mismo.
    Asegúrate de que los mensajes estén bien formateados y sean fáciles de leer en un dispositivo móvil.
    Si estas listando promociones o beneficios, asegurate de mencionar siempre si hay mas promociones o beneficios disponibles.
    """

    messages = [
        {"role": "system", "content": shortener_prompt},
        {"role": "user", "content": response},
    ]

    try:
        shortener_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        shortener_client = instructor.from_openai(shortener_client)
        shorter_responses, raw_response = (
            shortener_client.chat.completions.create_with_completion(
                model="gpt-4o",
                messages=messages,
                max_tokens=800,
                response_model=ShorterResponses,
            )
        )

        logger.info(f"Shorter responses: {shorter_responses.dict()}")

        update_token_count(raw_response)

        return shorter_responses.dict()["messages"]
    except Exception as e:
        logger.error(f"Error in get_shorter_responses: {e}")
        # If there's an error, return the original response as a single-item list
        return ShorterResponses(messages=[response]).dict()["messages"]


import uvicorn
import argparse
import asyncio


async def send_message(wa_client: WhatsApp, phone_number: str, message: str = ""):
    print(message)
    if message:
        wa_client.send_message(to=phone_number, text=message)
    else:
        response = await get_chatgpt_response(phone_number, "")
        print(response)

        if len(response) > 300:
            parts = await get_shorter_responses(response)
        else:
            parts = [response]

        logger.info(f"SENT,{phone_number},{response}")

        for part in parts:
            print(part)
            wa_client.send_message(to=phone_number, text=part)


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
                logger.info(f"Shorter response: {message}")
                yield message

    except Exception as e:
        logger.error(f"Error in get_shorter_responses: {e}")
        # If there's an error, yield the original response
        yield {"role": "assistant", "content": response}


async def generate_response(
    message: Message,
    conversation_history: ConversationHistory,
    phone_number: str,
    timezone: str = "America/Lima",
    system_prompt: str = "You are a helpful assistant.",
    model: str = "gpt-4o",
    max_message_chars: int = 300,
    openai_client: AsyncOpenAI = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY")),
) -> List[Dict[str, str]]:
    """
    Requests a response from OpenAI based on the input message and conversation history.
    """
    current_time = datetime.now()
    local_time = current_time.astimezone(ZoneInfo(timezone))
    formatted_date = local_time.date().isoformat()

    system_prompt_formatted = (
        system_prompt
        + f"Today's date is {formatted_date}. "
        + f"The user's phone number is: {phone_number}. "
        f"The user's name is: {message.from_user.name}."
    )

    messages = [
        {"role": "system", "content": system_prompt_formatted},
    ]
    messages.extend(await conversation_history[phone_number])
    messages.append({"role": "user", "content": message.text})

    response = await openai_client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=800,
    )

    content = (
        response.choices[0].message.content.strip()
        if response.choices[0].message.content
        else "I'm sorry, I couldn't retrieve the requested information."
    )

    if len(content) > max_message_chars:
        return [
            {"role": "assistant", "content": msg}
            for msg in await get_shorter_responses(content)
        ]
    else:
        return [{"role": "assistant", "content": content}]
