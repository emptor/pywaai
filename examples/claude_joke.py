import os
import logging
import flask  # pip3 install flask
from claudette import Chat, models
from pywa import WhatsApp, filters
from pywa.types import Message

flask_app = flask.Flask(__name__)

# Make sure to replace these with your actual credentials
wa = WhatsApp(
    phone_id="your_phone_number",
    token="your_token",
    server=flask_app,
    verify_token="xyzxyz",
)

# Initialize the Chat object with Claude model
chat = Chat(models[1], sp="You are a funny joke-telling bot.")

async def get_joke() -> str:
    """
    Requests a joke from Claude.
    """
    response = chat("Tell me a joke.")
    return response

@wa.on_message(filters.command("joke"))
async def tell_joke(_: WhatsApp, msg: Message):
    """
    Handles the "/joke" command to send a joke from Claude.
    """
    try:
        joke = await get_joke()
        msg.reply_text(joke)
    except Exception as e:
        logging.error(f"Error getting joke from Claude: {e}")
        msg.reply_text("Sorry, I couldn't find a joke right now. Try again later.")

# Run the server
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    flask_app.run(debug=True)