from openai import OpenAI
import instructor
from pydantic import BaseModel, Field
from typing import List
import os
from pywa import WhatsApp
from typing import List, Dict, Optional, Type
from .conversation_db import ConversationHistory
from datetime import datetime
from zoneinfo import ZoneInfo
from openai import AsyncOpenAI
from instructor import OpenAISchema

try:
    import logfire as logger
    logger.configure()
except ImportError:
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


async def get_shorter_responses(response: str) -> List[str]:
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
        return [response]


async def send_message(wa_client: WhatsApp, phone_number: str, message: str = ""):
    print(message)
    if message:
        wa_client.send_message(to=phone_number, text=message)
    else:
        responses = await generate_response(
            conversation_history=ConversationHistory(),
            phone_number=phone_number,
            message_text="",
            user_name="",
            system_prompt="You are a helpful assistant.",
            model="gpt-4o",
        )

        for response in responses:
            print(response["content"])
            wa_client.send_message(to=phone_number, text=response["content"])
            logger.info(f"SENT,{phone_number},{response['content']}")


async def execute_tools(tool_calls, tool_functions):
    results = []
    for call in tool_calls:
        for func in tool_functions:
            if func.__name__ == call.function.name:
                args = eval(call.function.arguments)
                result = func(**args).run()
                results.append(result)
    return results if results else None


async def generate_response(
    conversation_history: ConversationHistory,
    phone_number: str,
    message_text: str,
    user_name: str,
    timezone: str = "America/Lima",
    system_prompt: str = "You are a helpful assistant.",
    model: str = "gpt-4o",
    max_message_chars: int = 300,
    openai_client: AsyncOpenAI = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY")),
    tool_functions: Optional[List[Type[OpenAISchema]]] = None,
) -> List[Dict[str, str]]:
    """
    Requests a response from OpenAI based on the input message and conversation history.
    """
    current_time = datetime.now()
    local_time = current_time.astimezone(ZoneInfo(timezone))
    formatted_date = local_time.date().isoformat()

    system_prompt_formatted = (
        system_prompt
        + f" Today's date is {formatted_date}."
        + f" The user's phone number is: {phone_number}."
        + f" The user's name is: {user_name}."
    )

    messages = [
        {"role": "system", "content": system_prompt_formatted},
    ]
    messages.extend(await conversation_history[phone_number])
    messages.append({"role": "user", "content": message_text})

    chat_completion_kwargs = {
        "model": model,
        "messages": messages,
        "max_tokens": 800,
    }

    if tool_functions:
        chat_completion_kwargs["tools"] = [
            {"type": "function", "function": func.openai_schema}
            for func in tool_functions
        ]
        chat_completion_kwargs["tool_choice"] = "auto"

    response = await openai_client.chat.completions.create(**chat_completion_kwargs)

    if response.choices[0].message.tool_calls:
        tool_calls = response.choices[0].message.tool_calls
        assistant_responses = await execute_tools(tool_calls, tool_functions)

        for i, tool_call in enumerate(tool_calls):
            await conversation_history.append(
                phone_number,
                {"role": "assistant", "tool_calls": [tool_call.model_dump()]},
            )
            await conversation_history.append(
                phone_number,
                {
                    "role": "tool",
                    "content": assistant_responses[i],
                    "tool_call_id": tool_call.id,
                },
            )

        messages = [
            {"role": "system", "content": system_prompt_formatted}
        ] + await conversation_history[phone_number]
        messages.append({"role": "user", "content": message_text})

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
        shorter_responses = await get_shorter_responses(content)
        return [{"role": "assistant", "content": msg} for msg in shorter_responses]
    else:
        return [{"role": "assistant", "content": content}]
