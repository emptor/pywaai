import os
import logging
import flask
from openai import AsyncOpenAI
from pywa import WhatsApp
from pywa.types import Message
import jsonlines
from collections import defaultdict
import json
import instructor
from pydantic import BaseModel, Field
from typing import List, AsyncGenerator

flask_app = flask.Flask(__name__)

# Make sure to replace these with your actual credentials
wa = WhatsApp(
    phone_id="your_phone_number",
    token="your_token",
    server=flask_app,
    verify_token="xyzxyz",
)

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


class ShorterResponses(BaseModel):
    """A rewritten list of messages based on the original response, but more succint, interesting and modular across multiple messages."""
    messages: List[str] = Field(..., description="A list of 2-4 shorter messages")

    def dict(self):
        return {"messages": [message for message in self.messages]}


async def get_shorter_responses(response: str, client: WhatsApp, msg: Message) -> AsyncGenerator[str, None]:
    shortener_prompt = """
    You are an assistant tasked with splitting a long message into 2-4 shorter WhatsApp-friendly messages.
    Each message should be self-contained and coherent.
    Ensure messages are well-formatted and easy to read on mobile devices.
    """
    messages = [
        {"role": "system", "content": shortener_prompt},
        {"role": "user", "content": response}
    ]

    try:
        shortener_client = instructor.patch(openai_client)
        
        stream = shortener_client.chat.completions.create_partial(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=800,
            response_model=ShorterResponses,
            stream=True
        )

        async for shorter_response in stream:
            message = shorter_response.message
            logging.info(f"Shorter response: {message}")
            client.send_message(to=msg.from_user.wa_id, text=message)
            yield message

    except Exception as e:
        logging.error(f"Error in get_shorter_responses: {e}")
        # If there's an error, yield the original response
        client.send_message(to=msg.from_user.wa_id, text=response)
        yield response

async def get_openai_response(message: str, phone_number: str) -> str:
    """
    Requests a response from OpenAI based on the input message and conversation history.
    """
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
    ] + [
        {"role": "user", "content": message},
    ]

    response = await openai_client.chat.completions.create(
        model="gpt-4o-latest",  
        messages=messages,
        max_tokens=300,  # Increased to allow for longer initial responses
    )
    return response.choices[0].message.content.strip()

@wa.on_message()
async def respond_message(client: WhatsApp, msg: Message):
    """
    Handles all incoming messages, moderates them, and responds with an OpenAI-generated response.
    """
    try:
        if msg.text is None:
            raise ValueError("Message text is None")
        
        response = await get_openai_response(msg.text, msg.from_user.wa_id)
        
        if len(response) > 300:
            async for part in get_shorter_responses(response, client, msg):
                logging.info(f"SENT,{msg.from_user.wa_id},{part}")
        else:
            client.send_message(to=msg.from_user.wa_id, text=response)
            logging.info(f"SENT,{msg.from_user.wa_id},{response}")

    except ValueError as ve:
        logging.error(f"ValueError: {ve}")
        client.send_message(to=msg.from_user.wa_id, text="Sorry, I couldn't process your message. Please try again.")
    except Exception as e:
        logging.error(f"Error processing message: {e}")
        client.send_message(to=msg.from_user.wa_id, text="Sorry, I couldn't generate a response right now. Please try again later.")

# Run the server
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    flask_app.run(debug=True)