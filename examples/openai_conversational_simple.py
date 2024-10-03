import os
import logging
import flask
from openai import AsyncOpenAI
from pywa import WhatsApp
from pywa.types import Message

flask_app = flask.Flask(__name__)

mng = os.environ.get("WHATSAPP_MANAGER_TOKEN")


app = FastAPI()


wa = WhatsApp(
    token=mng,
    phone_id=phone_id,
    app_id=app_id,
    app_secret=app_secret,  # Required for validation
    server=app,
    verify_token=verify_token,
    callback_url=callback_url,  # Replace with your public callback URL
    business_account_id=business_account_id,
    verify_timeout=10,
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
        msg.reply_text(
            "Sorry, I couldn't generate a response right now. Please try again later."
        )


# Run the server
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    flask_app.run(debug=True)
