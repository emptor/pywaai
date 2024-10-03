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
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any


flask_app = flask.Flask(__name__)

# Make sure to replace these with your actual credentials
wa = WhatsApp(
    phone_id="your_phone_number",
    token="your_token",
    server=flask_app,
    verify_token="xyzxyz",
)

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
google_geocode_api_key = os.environ.get("GOOGLE_GEOCODE_API_KEY")

class GeocodeResponse(BaseModel):
    lat: float
    lng: float
    formatted_address: str
    raw_data: Dict[str, Any]

async def get_location_from_address(address: str) -> Optional[GeocodeResponse]:
    url = f"https://maps.googleapis.com/maps/api/geocode/json?language=es&address={address}&key={google_geocode_api_key}"
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
    data = response.json()
    if data['status'] == 'OK':
        result = data['results'][0]
        location = result['geometry']['location']
        formatted_address = result['formatted_address']
        return GeocodeResponse(
            lat=location['lat'],
            lng=location['lng'],
            formatted_address=formatted_address,
            raw_data=data
        )
    else:
        return None

class SearchLocation(BaseModel):
    """
    Obtiene la ubicación de una dirección o un lugar.
    """
    address: str = Field(..., description="La dirección o descripción del lugar")

    async def run(self):
        geocode_response = await get_location_from_address(self.address)
        
        if geocode_response is None:
            return json.dumps({
                "error": "No se pudo encontrar la ubicación"
            })

        return json.dumps({
            "latitude": geocode_response.lat,
            "longitude": geocode_response.lng,
            "name": self.address,
            "address": geocode_response.formatted_address
        })

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

conversation_history = defaultdict(list)

async def get_openai_response(message: str, phone_number: str) -> str:
    """
    Requests a response from OpenAI based on the input message and conversation history.
    """
    conversation: List[Dict[str, Any]] = conversation_history[phone_number]    

    system_prompt = "You are a helpful assistant."

    messages = [
        {"role": "system", "content": system_prompt},
    ] + conversation + [
        {"role": "user", "content": message},
    ]

    response = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        tools=[{"type": "function", "function": func.model_json_schema()} for func in tool_functions],
        tool_choice="auto",
        max_tokens=800,
    )

    if response.choices[0].message.tool_calls:
        tool_calls = response.choices[0].message.tool_calls
        assistant_responses = await execute_tools(tool_calls, tool_functions)
        
        for i, tool_call in enumerate(tool_calls):
            conversation_history.append(phone_number, {
                "role": "assistant", 
                "content": None,
                "tool_calls": [tool_call.model_dump()]
            })
            
            conversation_history.append(phone_number, {
                "role": "tool",
                "content": assistant_responses[i],
                "tool_call_id": tool_call.id
            })

        messages = [{"role": "system", "content": system_prompt}] + conversation_history[phone_number]
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=800,
        )

    return response.choices[0].message.content.strip() if response.choices[0].message.content else "I'm sorry, I couldn't retrieve the requested information."

@wa.on_message()
async def respond_message(client: WhatsApp, msg: Message):
    """
    Handles all incoming messages, moderates them, and responds with an OpenAI-generated response.
    """
    try:
        if msg.text is None:
            raise ValueError("Message text is None")
        
        conversation_history.append(msg.from_user.wa_id, {"role": "user", "content": msg.text})
        response = await get_openai_response(msg.text, msg.from_user.wa_id)
        
        msg.reply_text(text=response)
        conversation_history.append(msg.from_user.wa_id, {"role": "assistant", "content": response})
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