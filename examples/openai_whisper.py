import os
import logging
import flask
from openai import AsyncOpenAI
from pywa import WhatsApp
from pywa.types import Message
from io import BytesIO

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
    Handles incoming audio messages, transcribes them, and responds with the transcription.
    """
    try:
        if msg.type != "audio":
            msg.reply_text(text="Please send an audio message for transcription.")
            return

        audio_content = msg.audio.download(in_memory=True)

        # Use BytesIO so we can keep things in-memory
        audio_buffer = BytesIO(audio_content)
        audio_buffer.name = "audio.ogg"

        mime_type = "audio/ogg; codecs=opus"

        transcription = await openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=("audio.ogg", audio_buffer, mime_type),
            language="es"
        )

        logging.info(f"Transcribed text: {transcription.text}")

        # Reply with the transcription
        msg.reply_text(text=f"{transcription.text}")

    except Exception as e:
        logging.error(f"Error processing audio message: {e}")
        msg.reply_text(text="Sorry, I couldn't transcribe your audio message. Please try again later.")

# Run the server
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    flask_app.run(debug=True)