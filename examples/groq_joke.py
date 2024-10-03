import os
import logging
import flask  # pip3 install flask
from groq import Groq
from pywa import WhatsApp, filters
from pywa.types import Message

flask_app = flask.Flask(__name__)

# Replace with your actual WhatsApp credentials
wa = WhatsApp(
    phone_id="your_phone_number",
    token="your_token",
    server=flask_app,
    verify_token="xyzxyz",
)

# Configure Groq
client = Groq(api_key=os.environ["GROQ_API_KEY"])  

async def get_joke() -> str:
    """
    Requests a joke from a Groq model.
    """
    completion = client.chat.completions.create(
        model="llama3-70b-8192",  # Or another Groq model like mixtral-8x7b-32768
        messages=[
            {"role": "system", "content": "You are a funny joke-telling bot."},
            {"role": "user", "content": "Tell me a joke."},
        ],
        temperature=1,
        max_tokens=256,  # Adjust as needed 
        top_p=1,
    )

    # Gather the full joke (non-streaming)
    full_joke = ""
    async for chunk in completion:
        full_joke += chunk.choices[0].delta.content or ""
    return full_joke.strip()

@wa.on_message(filters.command("joke"))
async def tell_joke(_: WhatsApp, msg: Message):
    """
    Handles the "/joke" command and sends a joke from Groq.
    """
    try:
        joke = await get_joke()
        msg.reply_text(joke)
    except Exception as e:
        logging.error(f"Error getting joke from Groq: {e}")
        msg.reply_text("Sorry, I couldn't find a joke right now. Try again later.")

# Run the server
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    flask_app.run(debug=True)