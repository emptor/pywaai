"""
This script implements a WhatsApp bot using FastAPI and OpenAI's GPT model.

Key features:
- Handles incoming WhatsApp messages
- Generates responses using OpenAI's GPT model
- Maintains conversation history for each user
- Provides an API endpoint to append messages to conversations

API Usage:
POST /conversation/{conversation_ulid}

Example requests:

1. Using curl:
   curl -X POST "http://localhost:8080/conversation/01H1VECZJX2ZNVN1VN1QG0JHGX" \
   -H "Authorization: your_api_key_here" \
   -H "Content-Type: application/json" \
   -d '{"message": "Hello, how are you?", "trigger_generation": true}'

2. Using httpx in Python:
   import httpx

   url = "http://localhost:8080/conversation/01H1VECZJX2ZNVN1VN1QG0JHGX"
   headers = {
       "Authorization": "your_api_key_here",
       "Content-Type": "application/json"
   }
   data = {
       "message": "Hello, how are you?",
       "trigger_generation": True
   }

   response = httpx.post(url, json=data, headers=headers)
   print(response.json())
"""

# Import necessary libraries
import os
from ulid import ULID
from openai import AsyncOpenAI
from pywa import WhatsApp
from pywa.types import Message
import asyncio
from datetime import datetime
from asyncio import Task
from typing import Any, Dict, Tuple
from fastapi import FastAPI, HTTPException, Depends, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
import argparse
import uvicorn
from loguru import logger

# Set up environment variables
WA_TOKEN = os.environ.get("WHATSAPP_MANAGER_TOKEN")
WEBHOOK_API_KEY = os.environ.get("WHATSAPP_WEBHOOK_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# Initialize FastAPI app
app = FastAPI()


# Initialize WhatsApp client
wa = WhatsApp(
    token=WA_TOKEN,
    phone_id=phone_id,
    app_id=app_id,
    app_secret=app_secret,  # Required for validation
    server=app,
    verify_token=verify_token,
    callback_url=callback_url,  # Replace with your public callback URL
    business_account_id=business_account_id,
    verify_timeout=10,
)

# Initialize OpenAI client
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Initialize conversation history
conversation_history: Dict[str, Tuple[str, list[dict[str, str]]]] = {}

# Store the last message timestamp for each user
last_message_time: dict[str, datetime] = {}

# Store the tasks for each user
user_tasks: dict[str, Task[Any]] = {}

# API Key security
if not WEBHOOK_API_KEY:
    logger.warning(
        "WHATSAPP_WEBHOOK_API_KEY is not set. This may cause authentication issues."
    )

# Use APIKeyHeader for authentication
api_key_header = APIKeyHeader(name="Authorization", auto_error=False)


# Function to validate API key
async def get_api_key(api_key: str = Security(api_key_header)):
    if api_key == WEBHOOK_API_KEY:
        return api_key
    raise HTTPException(status_code=403, detail="Could not validate credentials")


# Function to get or create a conversation
def get_or_create_conversation(phone_number: str) -> Tuple[str, list[dict[str, str]]]:
    if phone_number not in conversation_history:
        new_ulid = str(ULID())
        conversation_history[phone_number] = (new_ulid, [])
        logger.info(f"New conversation created: ULID={new_ulid}, Phone={phone_number}")

    return conversation_history[phone_number]


# Function to get OpenAI response
async def get_openai_response(message: str, phone_number: str) -> str:
    """
    Requests a response from OpenAI based on the input message and conversation history.
    """
    _, conversation = get_or_create_conversation(phone_number)
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


# Function to generate a new message from OpenAI
async def get_openai_generation(phone_number: str) -> str:
    """
    Generates a new message for the thread based on the conversation history.
    """
    _, conversation = get_or_create_conversation(phone_number)
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
    ] + conversation

    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        max_tokens=300,
    )
    return response.choices[0].message.content.strip()


# Function to process and respond to messages
async def process_and_respond(client: WhatsApp, msg: Message):
    """
    Process the message and send a response after a delay.
    """
    try:
        if msg.text is None:
            raise ValueError("Message text is None")

        _, conversation = get_or_create_conversation(msg.from_user.wa_id)
        conversation.append({"role": "user", "content": msg.text})

        # Wait for 5 seconds before processing
        await asyncio.sleep(5)

        # Check if this task has been cancelled
        current_task = asyncio.current_task()
        if current_task and current_task.cancelled():
            return

        response = await get_openai_response(msg.text, msg.from_user.wa_id)

        client.send_message(to=msg.from_user.wa_id, text=response)
        conversation.append({"role": "assistant", "content": response})
        logger.info(f"SENT,{msg.from_user.wa_id},{response}")

    except ValueError as ve:
        logger.error(f"ValueError: {ve}")
        client.send_message(
            to=msg.from_user.wa_id,
            text="Sorry, I couldn't process your message. Please try again.",
        )
    except asyncio.CancelledError:
        logger.info(f"Task cancelled for user {msg.from_user.wa_id}")
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        client.send_message(
            to=msg.from_user.wa_id,
            text="Sorry, I couldn't generate a response right now. Please try again later.",
        )


# TODO: Simplify this, we don't need the complex handler as this is webhook example
# WhatsApp message handler
@wa.on_message()
async def respond_message(client: WhatsApp, msg: Message):
    pass


# Pydantic model for message input
class MessageInput(BaseModel):
    message: str


# API endpoint to append messages to conversations
@app.post("/conversation/{conversation_ulid}")
async def append_to_conversation(
    conversation_ulid: str,
    message_input: MessageInput,
    trigger_generation: bool = False,
    api_key: str = Depends(get_api_key),
):
    # Iterate through all conversations in the conversation_history
    for phone_number, (ulid, conversation) in conversation_history.items():
        # Check if the current conversation matches the requested ULID
        if ulid == conversation_ulid:
            # Append the new message to the conversation
            conversation.append({"role": "system", "content": message_input.message})
            # Log the action
            logger.info(
                f"Appended system message to conversation: ULID={conversation_ulid}"
            )
            # Print the updated conversation (for debugging purposes)
            print(conversation)

            # Check if we should generate a response
            if trigger_generation:
                try:
                    # Generate a response using OpenAI
                    response = await get_openai_generation(phone_number)
                    # Send the generated response to the user via WhatsApp
                    wa.send_message(to=phone_number, text=response)
                    # Append the assistant's response to the conversation
                    conversation.append({"role": "assistant", "content": response})
                    # Log the sent message
                    logger.info(f"SENT,{phone_number},{response}")
                    # Return a success message
                    return {
                        "status": "success",
                        "message": "Message appended and response sent",
                    }
                except Exception as e:
                    # Log any errors that occur during response generation
                    logger.error(f"Error generating response: {e}")
                    # Return an error message
                    return {"status": "error", "message": "Failed to generate response"}

            # If no response generation was triggered, return a success message
            return {"status": "success", "message": "Message appended to conversation"}

    # If the conversation ULID was not found, raise a 404 error
    raise HTTPException(status_code=404, detail="Conversation not found")


# Function to start the server
def start_server():
    uvicorn.run(app, host="0.0.0.0", port=8080)


# Main entry point
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WhatsApp Bot")
    parser.add_argument("action", choices=["start"], help="Action to perform")
    args = parser.parse_args()
    if args.action == "start":
        start_server()
