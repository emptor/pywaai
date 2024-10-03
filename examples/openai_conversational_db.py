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


class ConversationHistory:
    def __init__(self, db_path: str = "conversations.db", key: bytes = None):
        self.db_path = db_path
        self.key = key or os.urandom(32)
        self.init_db()

    def init_db(self):
        with self.get_connection() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS conversations
                             (phone_number TEXT, message TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"""
            )

    @contextmanager
    def get_connection(self):
        conn = sqlcipher.connect(self.db_path)
        conn.execute(f"PRAGMA key = '{self.key.hex()}'")
        try:
            yield conn
        finally:
            conn.close()

    def __getitem__(self, phone_number: str) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT message FROM conversations WHERE phone_number=? ORDER BY timestamp",
                (phone_number,),
            )
            return [json.loads(row[0]) for row in cursor.fetchall()]

    def __setitem__(self, phone_number: str, value: List[Dict[str, Any]]):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM conversations WHERE phone_number=?", (phone_number,)
            )
            data = [(phone_number, json.dumps(message)) for message in value]
            cursor.executemany(
                "INSERT INTO conversations (phone_number, message) VALUES (?, ?)",
                data,
            )
            conn.commit()

    def append(self, phone_number: str, message: Dict[str, Any]):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO conversations (phone_number, message) VALUES (?, ?)",
                (phone_number, json.dumps(message)),
            )
            conn.commit()


# Initialize the ConversationHistory
conversation_history = ConversationHistory()


class ShorterResponses(BaseModel):
    """A rewritten list of messages based on the original response, but more succint, interesting and modular across multiple messages."""

    messages: List[str] = Field(..., description="A list of 2-4 shorter messages")

    def dict(self):
        return {"messages": [message for message in self.messages]}


google_geocode_api_key = os.environ.get("GOOGLE_GEOCODE_API_KEY")


async def get_location_from_address(address: str) -> tuple[float, float, str] | None:
    url = f"https://maps.googleapis.com/maps/api/geocode/json?language=es&address={address}&key={google_geocode_api_key}"
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
    data = response.json()
    if data["status"] == "OK":
        result = data["results"][0]
        location = result["geometry"]["location"]
        formatted_address = result["formatted_address"]
        print(data)
        return (location["lat"], location["lng"], formatted_address)
    else:
        return None


class SearchLocation(BaseModel):
    """
    Obtiene la ubicación de una dirección o un lugar.
    """

    address: str = Field(..., description="La dirección o descripción del lugar")

    def run(self):
        lat, long, formatted_address = get_location_from_address(self.address)

        if lat is None:
            return json.dumps({"error": "No se pudo encontrar la ubicación"})

        return json.dumps(
            {
                "latitude": lat,
                "longitude": long,
                "name": self.address,
                "address": formatted_address,
            }
        )


tool_functions = [SearchLocation]


async def execute_tools(tool_calls, tool_functions):
    results = []
    for call in tool_calls:
        for func in tool_functions:
            if func.__name__ == call.function.name:
                # Parse the arguments from the function call
                args = json.loads(call.function.arguments)
                # Create an instance of the class and run it
                result = func(**args).run()
                results.append(result)
    return results if results else None


async def get_shorter_responses(
    response: str, client: WhatsApp, msg: Message
) -> AsyncGenerator[str, None]:
    shortener_prompt = """
    You are an assistant tasked with splitting a long message into 2-4 shorter WhatsApp-friendly messages.
    Each message should be self-contained and coherent.
    Ensure messages are well-formatted and easy to read on mobile devices.
    """
    messages = [
        {"role": "system", "content": shortener_prompt},
        {"role": "user", "content": response},
    ]

    try:

        shortener_client = instructor.patch(openai_client)

        stream = shortener_client.chat.completions.create_partial(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=800,
            response_model=ShorterResponses,
            stream=True,
        )

        async for shorter_response in stream:
            for message in shorter_response.messages:
                logging.info(f"Shorter response: {message}")
                client.send_message(to=msg.from_user.wa_id, text=message)
                conversation_history.append(
                    msg.from_user.wa_id, {"role": "assistant", "content": message}
                )
                yield message

    except Exception as e:
        logging.error(f"Error in get_shorter_responses: {e}")
        # If there's an error, yield the original response
        client.send_message(to=msg.from_user.wa_id, text=response)
        conversation_history.append(
            msg.from_user.wa_id, {"role": "assistant", "content": response}
        )
        yield response


async def get_openai_response(message: str, phone_number: str) -> str:
    """
    Requests a response from OpenAI based on the input message and conversation history.
    """
    conversation = conversation_history[phone_number]
    current_time = datetime.now()
    peru_time = current_time.astimezone(ZoneInfo("America/Lima"))
    formatted_date = peru_time.date().isoformat()
    formatted_time = (
        peru_time.replace(
            minute=0 if peru_time.minute < 30 else 30, second=0, microsecond=0
        )
        .time()
        .isoformat(timespec="minutes")
    )

    system_prompt = f"You are a helpful assistant. Today's date is {formatted_date} and the current time is {formatted_time}. The user's phone number is: {phone_number}"

    messages = (
        [
            {"role": "system", "content": system_prompt},
        ]
        + conversation
        + [
            {"role": "user", "content": message},
        ]
    )

    response = await openai_client.chat.completions.create(
        model="gpt-4o-latest",
        messages=messages,
        tools=[
            {"type": "function", "function": func.model_json_schema()}
            for func in tool_functions
        ],
        tool_choice="auto",
        max_tokens=800,
    )

    if response.choices[0].message.tool_calls:
        tool_calls = response.choices[0].message.tool_calls
        assistant_responses = await execute_tools(tool_calls, tool_functions)

        for i, tool_call in enumerate(tool_calls):
            conversation_history.append(
                phone_number,
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call.model_dump()],
                },
            )

            conversation_history.append(
                phone_number,
                {
                    "role": "tool",
                    "content": assistant_responses[i],
                    "tool_call_id": tool_call.id,
                },
            )

        messages = [
            {"role": "system", "content": system_prompt}
        ] + conversation_history[phone_number]
        response = await openai_client.chat.completions.create(
            model="gpt-4o-latest",
            messages=messages,
            max_tokens=800,
        )

    return (
        response.choices[0].message.content.strip()
        if response.choices[0].message.content
        else "I'm sorry, I couldn't retrieve the requested information."
    )


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
            client.send_message(
                to=msg.from_user.wa_id,
                text="I'm sorry, but I can't respond to that kind of message.",
            )
            logging.warning(f"Flagged message from {msg.from_user.wa_id}: {msg.text}")
            return

        conversation_history.append(
            msg.from_user.wa_id, {"role": "user", "content": msg.text}
        )
        response = await get_openai_response(msg.text, msg.from_user.wa_id)

        if len(response) > 300:
            async for part in get_shorter_responses(response, client, msg):
                logging.info(f"SENT,{msg.from_user.wa_id},{part}")
        else:
            client.send_message(to=msg.from_user.wa_id, text=response)
            conversation_history.append(
                msg.from_user.wa_id, {"role": "assistant", "content": response}
            )
            logging.info(f"SENT,{msg.from_user.wa_id},{response}")

    except ValueError as ve:
        logging.error(f"ValueError: {ve}")
        client.send_message(
            to=msg.from_user.wa_id,
            text="Sorry, I couldn't process your message. Please try again.",
        )
    except Exception as e:
        logging.error(f"Error processing message: {e}")
        client.send_message(
            to=msg.from_user.wa_id,
            text="Sorry, I couldn't generate a response right now. Please try again later.",
        )


# Run the server
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    flask_app.run(debug=True)
