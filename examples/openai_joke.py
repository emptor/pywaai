import os
import logging
import flask  # pip3 install flask
from openai import AsyncOpenAI
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

client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


async def get_joke() -> str:
    """
    Requests a joke from ChatGPT.
    """
    response = await client.chat.completions.create(
        model="gpt-3.5-turbo",  # You can use gpt-4 if you have access
        messages=[
            {"role": "system", "content": "You are a funny joke-telling bot."},
            {"role": "user", "content": "Tell me a joke."},
        ],
        max_tokens=150,  # Adjust based on expected joke length
    )
    return response.choices[0].message.content.strip()


@wa.on_message(filters.command("joke"))
async def tell_joke(_: WhatsApp, msg: Message):
    """
    Handles the "/joke" command to send a joke from ChatGPT.
    """
    try:
        joke = await get_joke()
        msg.reply_text(joke)
    except Exception as e:
        logging.error(f"Error getting joke from ChatGPT: {e}")
        msg.reply_text("Sorry, I couldn't find a joke right now. Try again later.")


# Run the server
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    flask_app.run(debug=True)
