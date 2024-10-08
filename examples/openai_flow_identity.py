import os
from loguru import logger
from fastapi import FastAPI, HTTPException
import uvicorn
from openai import AsyncOpenAI
from pywa import WhatsApp
from pywa.types import Message, FlowButton, Template, FlowActionType
from pywa.types.flows import FlowRequest, FlowResponse, FlowStatus, FlowActionType
from pydantic import BaseModel
import uuid
from collections import defaultdict
import httpx
import asyncio

mng = os.environ.get("WA_TOKEN")
app = FastAPI()


phone_id = os.getenv("WA_PHONE_ID")

app_id = os.getenv("WA_APP_ID")
app_secret = os.getenv("WA_APP_SECRET")
verify_token = os.getenv("WA_VERIFY_TOKEN")
callback_url = f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN')}"
business_account_id = os.getenv("WA_BUSINESS_ACCOUNT_ID")

business_private_key = open(
    "/Users/gabrielpuliatti/code/soldev_new/soldev/ibk-private.pem"
).read()
business_private_key_password = os.getenv("IBK_PASSWORD")

wa = WhatsApp(
    token=mng,
    phone_id=phone_id,
    app_id="1655952435197468",
    app_secret=app_secret,  # Required for validation
    server=app,
    verify_token=verify_token,
    callback_url=callback_url,  # Replace with your public callback URL
    business_account_id="122194765761195",
    verify_timeout=10,
    business_private_key=business_private_key,
    business_private_key_password=business_private_key_password,
)
openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

wa.set_business_public_key(
    open("/Users/gabrielpuliatti/code/soldev_new/soldev/ibk.pem").read()
)


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
            flow_id="504095399172622",
            flow_token=flow_token,
            mode=FlowStatus.DRAFT,
            flow_action_type=FlowActionType.NAVIGATE,
            flow_action_screen="IDENTIFY",
        ),
    )


@wa.on_flow_request("/identify")
async def on_identify_request(
    _: WhatsApp, flow_request: FlowRequest
) -> FlowResponse | None:
    if flow_request.has_error:
        logger.error("Flow request has error: %s", flow_request.data)
        return
    else:
        data = flow_request.data
        print(data)
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

            print(person_id)
            phone_state.set_person_id(phone_number, person_id)

            logger.info(f"phone_state: {phone_state.get_state(phone_number)}")

            asyncio.create_task(poll_person_status(person_id, phone_number))

            async def send_delayed_message():
                """
                This function sends a message to the user after 30 seconds.
                This is so that the user has time to click on the "complete" button in the Flow.
                We probably should do something more robust here, like handling the flow completion in the callback.
                """
                await asyncio.sleep(30)
                wa.send_message(
                    to=phone_number,
                    text="Estamos validando la información entregada. Recibirá una notificación cuando el proceso esté completo.",
                )

            asyncio.create_task(send_delayed_message())

            return FlowResponse(
                version=flow_request.version,
                screen="LOGIN_SUCCESS",
                data={"document_id": document_id, "person_id": person_id},
            )


async def poll_person_status(person_id: str, phone_number: str):
    """
    Polls the status of a person_id every minute and notifies the user when the status changes.
    """
    api_key = os.getenv("GABRIEL_API_KEY") or ""
    headers = {
        "X-Api-Key": api_key,
        "accept": "application/json",
    }
    status_url = f"https://api.emptor.io/v3/pe/persons/{person_id}/status"

    while True:
        await asyncio.sleep(60)  # Wait for 60 seconds before each poll

        async with httpx.AsyncClient() as client:
            response = await client.get(status_url, headers=headers)
            if response.status_code != 200:
                logger.error(
                    f"Failed to fetch status for person_id {person_id}: {response.text}"
                )
                continue  # Retry on failure

            status_data = response.json()
            status = status_data.get("status")

            if status and status != "PENDING":
                if status == "INCOMPLETE":
                    wa.send_message(
                        to=phone_number,
                        text="No se pudo validar su identificación.",
                    )
                else:
                    wa.send_message(
                        to=phone_number,
                        text="Pudimos validar su identificación y le entregaremos los resultados al contratante.",
                    )
                logger.info(
                    f"Status for person_id {person_id} is {status}. Notified user."
                )
                break

            logger.info(f"Status for person_id {person_id} is still PENDING.")

    del phone_state.person_id_to_phone[person_id]


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


# Run the server
if __name__ == "__main__":
    example_phone_number = "51901171469"
    example_flow_token = str(uuid.uuid4())
    phone_state.add_flow_token(example_phone_number, example_flow_token)

    wa.send_template(
        to=example_phone_number,
        template=Template(
            name="basic_form_customer",
            language=Template.Language.SPANISH,
            header=Template.Image(
                image="https://i.ibb.co/kKypp0L/Solcito-Cartoon-1.png"
            ),
            body=[
                Template.TextValue(value="Walmart"),
            ],
            buttons=Template.FlowButton(
                flow_token=example_flow_token,
            ),
        ),
    )

    uvicorn.run(app, host="0.0.0.0", port=8080)
