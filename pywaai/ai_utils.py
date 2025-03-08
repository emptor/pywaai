from openai import OpenAI
import instructor
from pydantic import BaseModel, Field
from typing import List
import os
from pywa import WhatsApp
from typing import List, Dict, Optional, Type
from .conversation_db import ConversationManager
from datetime import datetime
from zoneinfo import ZoneInfo
from openai import AsyncOpenAI
from instructor import OpenAISchema
import httpx

async def get_access_token() -> str:
    """
    Get an access token from Auth0 for M2M authentication.
    
    Returns:
        str: The access token for making authenticated requests
    """
    AUTH0_DOMAIN = os.getenv("AUTH0_DOMAIN")
    AUTH0_APP_CLIENT_ID = os.getenv("AUTH0_APP_CLIENT_ID")
    AUTH0_APP_CLIENT_SECRET = os.getenv("AUTH0_APP_CLIENT_SECRET")
    AUDIENCE_IDENTIFIER = os.getenv("CONVERSATIONS_AUDIENCE_IDENTIFIER")
    
    payload = {
        "client_id": AUTH0_APP_CLIENT_ID,
        "client_secret": AUTH0_APP_CLIENT_SECRET,
        "audience": AUDIENCE_IDENTIFIER,
        "grant_type": "client_credentials"
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://{AUTH0_DOMAIN}/oauth/token",
            json=payload,
            headers={"Content-Type": "application/json"}
        )
        response.raise_for_status()
        return response.json()["access_token"]

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


import asyncio
import json

async def execute_tools(tool_calls, tool_functions):
    results = []
    for call in tool_calls:
        for func in tool_functions:
            if func.__name__ == call.function.name:
                args = json.loads(call.function.arguments)
                tool_instance = func(**args)
                
                # Call run() and get the result or coroutine
                potential_coroutine = tool_instance.run()
                
                # Check if the result is a coroutine
                if asyncio.iscoroutine(potential_coroutine):
                    # If it's a coroutine, await it
                    result = await potential_coroutine
                else:
                    # If it's not a coroutine, it's already the actual result
                    result = potential_coroutine
                    
                results.append(result)
    return results if results else None

class LocalOrRemoteConversation:
    """
    A helper class to abstract over using a local ConversationManager
    or a remote conversation API.
    """

    def __init__(
        self,
        phone_number: str,
        use_remote_api: bool = False,
        remote_base_url: Optional[str] = None,
        conversation_manager: Optional[ConversationManager] = None,
    ):
        self.phone_number = phone_number
        self.use_remote_api = use_remote_api
        self.remote_base_url = remote_base_url
        self.conversation_manager = conversation_manager
        self.conversation_id = None

    async def _get_or_create_conversation_id_local(self):
        conv = await self.conversation_manager.get_latest_conversation(
            self.phone_number
        )
        if not conv:
            # Create new conversation
            conv = await self.conversation_manager.create_conversation(
                self.phone_number
            )
        return conv.conversation_id

    async def _get_or_create_conversation_id_remote(self):
        token = await get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient() as client:
            url = f"{self.remote_base_url}/conversations/{self.phone_number}/latest"
            r = await client.get(url, headers=headers)
            if r.status_code == 404:
                # Create a new conversation
                create_url = f"{self.remote_base_url}/conversations/{self.phone_number}"
                c = await client.post(create_url, headers=headers)
                c.raise_for_status()
                return c.json()["conversation_id"]
            else:
                r.raise_for_status()
                return r.json()["conversation_id"]

    async def get_or_create_conversation_id(self):
        if self.conversation_id:
            return self.conversation_id

        if self.use_remote_api:
            self.conversation_id = await self._get_or_create_conversation_id_remote()
        else:
            self.conversation_id = await self._get_or_create_conversation_id_local()
        return self.conversation_id

    async def get_messages(self) -> List[Dict[str, str]]:
        cid = await self.get_or_create_conversation_id()
        if self.use_remote_api:
            token = await get_access_token()
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient() as client:
                url = f"{self.remote_base_url}/conversations/{self.phone_number}/{cid}/messages"
                r = await client.get(url, headers=headers)
                r.raise_for_status()
                return r.json()
        else:
            return await self.conversation_manager.get_messages(self.phone_number, cid)

    async def append_message(self, message: Dict[str, str]):
        cid = await self.get_or_create_conversation_id()
        if self.use_remote_api:
            token = await get_access_token()
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient() as client:
                url = f"{self.remote_base_url}/conversations/{self.phone_number}/{cid}/messages"
                r = await client.post(url, headers=headers, json=message)
                r.raise_for_status()
        else:
            await self.conversation_manager.add_message(self.phone_number, message, cid)

    async def append_tool(self, tool_call: dict, tool_content: str):
        """Append tool calls and tool responses to the conversation."""
        # tool_call is like {"role": "assistant", "tool_calls": [tool_call_data]}
        # tool_content is from the tool response
        await self.append_message(tool_call)
        
        # Get the tool_call_id from the tool_call
        # The structure should be properly accessed based on the model_dump() output
        if "tool_calls" in tool_call and isinstance(tool_call["tool_calls"], list) and len(tool_call["tool_calls"]) > 0:
            # Extract ID from the correct location in the structure
            if "id" in tool_call["tool_calls"][0]:
                tool_call_id = tool_call["tool_calls"][0]["id"]
            elif "function" in tool_call["tool_calls"][0] and "id" in tool_call["tool_calls"][0]:
                tool_call_id = tool_call["tool_calls"][0]["id"]
            else:
                # Directly access the structure as OpenAI returns it
                logger.debug(f"Tool call structure: {tool_call}")
                tool_call_id = tool_call["tool_calls"][0].get("id", None)
                if not tool_call_id:
                    logger.error(f"Cannot find id in tool_calls: {tool_call}")
                    raise ValueError("Missing id in tool_call structure")
        else:
            logger.error(f"Unexpected tool_call structure: {tool_call}")
            raise ValueError("Unexpected tool_call structure")
        
        # Append tool result message with the tool_call_id
        await self.append_message({
            "role": "tool", 
            "content": tool_content,
            "tool_call_id": tool_call_id
        })

async def generate_response(
    phone_number: str,
    message_text: str,
    user_name: str,
    timezone: str = "America/Lima",
    system_prompt: str = "You are a helpful assistant.",
    model: str = "gpt-4o",
    max_message_chars: int = 300,
    openai_client: AsyncOpenAI = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY")),
    tool_functions: Optional[List[Type[OpenAISchema]]] = None,
    use_remote_api: bool = False,
    remote_base_url: Optional[str] = None,
    conversation_manager: Optional[ConversationManager] = None,
) -> List[Dict[str, str]]:
    """
    Generate a response from the OpenAI model using either:
      - a local conversation database via ConversationManager
      - a remote conversation api

    If use_remote_api=True, remote_base_url must be provided.
    Otherwise conversation_manager must be provided.
    """
    if use_remote_api and not remote_base_url:
        raise ValueError("remote_base_url must be provided if use_remote_api=True")
    if not use_remote_api and not conversation_manager:
        raise ValueError(
            "conversation_manager must be provided if use_remote_api=False"
        )

    conv = LocalOrRemoteConversation(
        phone_number=phone_number,
        use_remote_api=use_remote_api,
        remote_base_url=remote_base_url,
        conversation_manager=conversation_manager,
    )

    # Append user message first
    await conv.append_message({"role": "user", "content": message_text})

    current_time = datetime.now()
    local_time = current_time.astimezone(ZoneInfo(timezone))
    formatted_date = local_time.date().isoformat()

    system_prompt_formatted = (
        system_prompt
        + f" Today's date is {formatted_date}."
        + f" The user's phone number is: {phone_number}."
        + f" The user's name is: {user_name}."
    )

    # Retrieve all conversation messages
    messages_history = await conv.get_messages()

    # Build the messages for openai
    messages = [
        {"role": "system", "content": system_prompt_formatted}
    ] + messages_history

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

    # Handle tool calls
    if response.choices[0].message.tool_calls:
        tool_calls = response.choices[0].message.tool_calls
        assistant_responses = await execute_tools(tool_calls, tool_functions)

        for i, tool_call in enumerate(tool_calls):
            await conv.append_tool(
                {"role": "assistant", "tool_calls": [tool_call.model_dump()]},
                assistant_responses[i],
            )

        # After tool calls, regenerate response
        messages_history = await conv.get_messages()
        logger.info(f"Messages history: {messages_history}")
        messages = [
            {"role": "system", "content": system_prompt_formatted}
        ] + messages_history

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

    # Possibly shorten response if too long
    if len(content) > max_message_chars:
        shorter_responses = await get_shorter_responses(content)
        # Append each shorter response to conversation and return
        final_responses = []
        for msg in shorter_responses:
            response_msg = {"role": "assistant", "content": msg}
            await conv.append_message(response_msg)
            final_responses.append(response_msg)
        return final_responses
    else:
        response_msg = {"role": "assistant", "content": content}
        await conv.append_message(response_msg)
        return [response_msg]
