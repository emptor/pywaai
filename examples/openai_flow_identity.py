import os
from loguru import logger
from fastapi import FastAPI, HTTPException
import uvicorn
from openai import AsyncOpenAI
from pywa import WhatsApp
from pywa.types import Message, FlowButton
from pywa.types.flows import FlowRequest, FlowResponse, FlowStatus, FlowActionType
from pydantic import BaseModel
import uuid
from collections import defaultdict
import httpx


mng = os.environ.get("WHATSAPP_API_KEY")
app = FastAPI()


business_private_key_password = os.getenv("IBK_PASSWORD")

wa = WhatsApp(
    token=mng,
    phone_id=phone_id,
    app_id=app_id,
    app_secret=app_secret,  # Required for validation
    server=app,
    verify_token=verify_token,
    callback_url=callback_url,  # Replace with your public callback URL
    business_account_id=business_account_id,
    # verify_timeout=10,
    business_private_key=business_private_key,
    business_private_key_password=business_private_key_password,
)
openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


# Replace the current phone_state with a class
class PhoneState:
    def __init__(self):
        self.state = defaultdict(lambda: {"flow_tokens": [], "person_id": ""})
        self.flow_token_to_phone = {}
        self.person_id_to_phone = {}

    def add_flow_token(self, phone_number: str, flow_token: str):
        self.state[phone_number]["flow_tokens"].append(flow_token)
        self.flow_token_to_phone[flow_token] = phone_number

    def set_person_id(self, phone_number: str, person_id: str):
        self.state[phone_number]["person_id"] = person_id
        self.person_id_to_phone[person_id] = phone_number

    def get_phone_by_flow_token(self, flow_token: str) -> str | None:
        return self.flow_token_to_phone.get(flow_token)

    def get_phone_by_person_id(self, person_id: str) -> str | None:
        return self.person_id_to_phone.get(person_id)

    def get_state(self, phone_number: str) -> dict:
        return self.state[phone_number]

    def get_flow_tokens(self, phone_number: str) -> list:
        return self.state[phone_number]["flow_tokens"]


# Initialize the new PhoneState
phone_state = PhoneState()


@wa.on_message()
async def respond_message(_: WhatsApp, msg: Message):
    """
    Handles all incoming messages and responds with an OpenAI-generated response.
    """
    phone_number = msg.from_user.wa_id
    flow_token = str(uuid.uuid4())
    phone_state.add_flow_token(phone_number, flow_token)

    wa.send_message(
        to=phone_number,
        text="Welcome to our app! Click the button below to login or sign up",
        buttons=FlowButton(
            title="Sign Up",
            flow_id="2859263487560485",
            flow_token=flow_token,
            mode=FlowStatus.DRAFT,
            flow_action_type=FlowActionType.NAVIGATE,
            flow_action_screen="IDENTIFY",
        ),
    )


@wa.on_flow_request("/identify-peru")
async def on_identify_request(
    _: WhatsApp, flow_request: FlowRequest
) -> FlowResponse | None:
    if flow_request.has_error:
        logger.error("Flow request has error: %s", flow_request.data)
        return
    else:
        data = flow_request.data
        if not isinstance(data, dict):
            logger.error("Flow request data is not a dictionary")
            return

        document_id = data.get("document_id")
        flow_token = flow_request.flow_token
        phone_number = phone_state.get_phone_by_flow_token(flow_token)

        if not document_id or not phone_number:
            logger.error("Document ID or phone number not found in flow request data")
            return

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.emptor.io/v3/pe/persons",
                headers={
                    # The API key has the /callback webhook configured as part of the account
                    "X-Api-Key": os.getenv("GABRIEL_API_KEY") or "",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                json={
                    "city_locode": "PE LIM",
                    "document_id": document_id,
                },
            )
            person_id = response.json()
            if not person_id:
                logger.error("Person ID not found in API response")
                return

            # Save the mapping of phone number to person_id
            phone_state.set_person_id(phone_number, person_id)

            logger.info(f"phone_state: {phone_state.get_state(phone_number)}")

            return FlowResponse(
                version=flow_request.version,
                screen="LOGIN_SUCCESS",
                data={"document_id": document_id, "person_id": person_id},
            )


class CallbackRequest(BaseModel):
    webhook_request_id: str
    timestamp: str
    _links: dict
    id: str
    status: str
    custom_data: dict | None
    created_at: str
    updated_at: str
    reports: dict


@app.post("/callback")
async def callback(request: CallbackRequest):
    """
    Webhook callback endpoint to handle the response from bgcapi.
    """
    print(request.dict())
    try:
        person_id = request.id
        status = request.status

        # Find the phone number associated with the person_id
        phone_number = phone_state.get_phone_by_person_id(person_id)
        if phone_number:
            # Send the status back to the user
            wa.send_message(to=phone_number, text=f"Status: {status}")
            return {"status": "success", "message": "Status sent to user"}
        return {"status": "error", "message": "Conversation not found"}
    except HTTPException as e:
        logger.error(f"Error in callback: {e.status_code}: {e.detail}")
        return {"status": "error", "message": "Conversation not found"}
    except Exception as e:
        logger.error(f"Error in callback: {e}")
        return {"status": "error", "message": "Internal Server Error"}


# Run the server
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
