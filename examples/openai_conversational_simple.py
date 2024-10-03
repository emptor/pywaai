import os
import logging
import flask
from openai import AsyncOpenAI
from pywa import WhatsApp
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

async def get_openai_response(message: str) -> str:
    """
    Requests a response from OpenAI based on the input message.
    """
    response = await client.chat.completions.create(
        model="gpt-4o-latest",  
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": message},
        ],
        max_tokens=150,  # Adjust based on expected response length
    )
    return response.choices[0].message.content.strip()

@wa.on_message()
async def respond_message(_: WhatsApp, msg: Message):
    """
    Handles all incoming messages and responds with an OpenAI-generated response.
    """
    try:
        response = await get_openai_response(msg.text)
        msg.reply_text(response)
    except Exception as e:
        logging.error(f"Error getting response from OpenAI: {e}")
        msg.reply_text("Sorry, I couldn't generate a response right now. Please try again later.")

# Run the server
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    flask_app.run(debug=True)