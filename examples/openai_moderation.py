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
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo

flask_app = flask.Flask(__name__)

# Make sure to replace these with your actual credentials
wa = WhatsApp(
    phone_id="your_phone_number",
    token="your_token",
    server=flask_app,
    verify_token="xyzxyz",
)

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

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
            msg.reply_text(text="I'm sorry, but I can't respond to that kind of message.")
            logging.warning(f"Flagged message from {msg.from_user.wa_id}: {msg.text}")
            return
        else:
            msg.reply_text(text="Your message is fine.")


    except ValueError as ve:
        logging.error(f"ValueError: {ve}")
        msg.reply_text(text="Sorry, I couldn't process your message. Please try again.")
    except Exception as e:
        logging.error(f"Error processing message: {e}")
        msg.reply_text(text="Sorry, I couldn't generate a response right now. Please try again later.")

# Run the server
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    flask_app.run(debug=True)