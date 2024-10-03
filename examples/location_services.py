import os
import flask
import logging
import httpx
from openai import AsyncOpenAI
from pywa import WhatsApp
from pywa.types import Message
import jsonlines
from collections import defaultdict
import json
from pydantic import BaseModel, Field
from typing import List, Dict, Optional, Any

flask_app = flask.Flask(__name__)

# Replace these with your actual credentials
wa = WhatsApp(
    phone_id="your_phone_number",
    token="your_token",
    server=flask_app,
    verify_token="xyzxyz",
)

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

class ConsulateInfo(BaseModel):
    country: str
    city: str
    address: str
    phone: str
    latitude: float
    longitude: float

class EmergencyService(BaseModel):
    type: str
    name: str
    address: str
    phone: str
    latitude: float
    longitude: float

async def find_nearby_consulates(latitude: float, longitude: float) -> List[ConsulateInfo]:
    """ Simulate fetching nearby consulates based on latitude and longitude """
    # This is a placeholder for actual data retrieval logic
    return [
        ConsulateInfo(
            country="Country A",
            city="City X",
            address="1234 Diplomat St.",
            phone="+123456789",
            latitude=latitude + 0.01,  # simulated nearby location
            longitude=longitude - 0.01
        ),
        ConsulateInfo(
            country="Country B",
            city="City Y",
            address="5678 Embassy Ave.",
            phone="+987654321",
            latitude=latitude - 0.01,  # simulated nearby location
            longitude=longitude + 0.01
        )
    ]

async def find_nearby_emergency_services(latitude: float, longitude: float) -> List[EmergencyService]:
    """ Simulate fetching nearby emergency services based on latitude and longitude """
    return [
        EmergencyService(
            type="Hospital",
            name="MediCare Clinic",
            address="112 Emergency Rd.",
            phone="112",
            latitude=latitude + 0.005,  # simulated nearby location
            longitude=longitude + 0.005
        ),
        EmergencyService(
            type="Police Station",
            name="Central Police Dept",
            address="911 Safety Blvd.",
            phone="911",
            latitude=latitude - 0.005,  # simulated nearby location
            longitude=longitude - 0.005
        )
    ]

async def respond_with_nearby_services(latitude: float, longitude: float, client: WhatsApp, wa_id: str):
    consulates = await find_nearby_consulates(latitude, longitude)
    emergency_services = await find_nearby_emergency_services(latitude, longitude)

    response_message = "Nearby Consulates:\n"
    for consulate in consulates:
        response_message += f"{consulate.country} - {consulate.city}: {consulate.address}, Tel: {consulate.phone}\n"

    response_message += "\nNearby Emergency Services:\n"
    for service in emergency_services:
        response_message += f"{service.type}: {service.name}, Address: {service.address}, Tel: {service.phone}\n"

    client.send_message(to=wa_id, text=response_message)

@wa.on_message()
async def handle_location_message(client: WhatsApp, msg: Message):
    if msg.location:
        logging.info(f"Location received: {msg.location.latitude}, {msg.location.longitude}")
        await respond_with_nearby_services(msg.location.latitude, msg.location.longitude, client, msg.from_user.wa_id)
    else:
        logging.error("Message did not contain location data")

# Run the Flask server
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    flask_app.run(debug=True)
