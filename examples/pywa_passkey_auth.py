import os
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pywa import WhatsApp
from pywa.types import Message
from pydantic import BaseModel
from typing import Dict
from loguru import logger
import secrets
import time
from webauthn import generate_registration_options, verify_registration_response
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    UserVerificationRequirement,
    PublicKeyCredentialRpEntity,
    PublicKeyCredentialUserEntity,
)
import base64
import json
from typing import Any

# Environment variables
WA_TOKEN = os.environ.get("WHATSAPP_MANAGER_TOKEN")

# Initialize FastAPI app
app = FastAPI()


# Initialize WhatsApp client
wa = WhatsApp(
    token=WA_TOKEN,
    phone_id=phone_id,
    app_id=app_id,
    app_secret=app_secret,
    server=app,
    verify_token=verify_token,
    callback_url=callback_url,
    business_account_id=business_account_id,
    verify_timeout=verify_timeout,
)

# Store user information
users: Dict[str, Dict[str, Any]] = {}

# Store login request information
login_requests: Dict[str, Dict[str, Any]] = {}


class PasskeyRegistrationOptions(BaseModel):
    phone_number: str


class PasskeyRegistrationResponse(BaseModel):
    phone_number: str
    credential: Dict[str, Any]


@app.get("/generate_passkey")
async def generate_passkey(
    phone_number: str = Query(..., description="The user's phone number")
):
    user_id = secrets.token_bytes(32)
    login_id = secrets.token_urlsafe(16)

    registration_options = generate_registration_options(
        rp_id=callback_url,
        rp_name="WhatsApp Chatbot",
        user_id=user_id,
        user_name=phone_number,
        user_display_name=phone_number,
        attestation="direct",
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.REQUIRED
        ),
        supported_pub_key_algs=[COSEAlgorithmIdentifier.ECDSA_SHA_256],
    )

    login_requests[login_id] = {
        "phone_number": phone_number,
        "challenge": registration_options.challenge,
        "user_id": user_id.hex(),
        "timestamp": time.time(),
    }

    # Serialize registration options for client-side use
    serialized_options = {
        "challenge": base64.b64encode(registration_options.challenge).decode(),
        "rp": {
            "name": registration_options.rp.name,
            "id": registration_options.rp.id,
        },
        "user": {
            "id": base64.b64encode(registration_options.user.id).decode(),
            "name": registration_options.user.name,
            "displayName": registration_options.user.display_name,
        },
        "pubKeyCredParams": [
            {"type": "public-key", "alg": alg.value}
            for alg in registration_options.pub_key_cred_params
        ],
        "timeout": registration_options.timeout,
        "attestation": registration_options.attestation,
        "authenticatorSelection": {
            "userVerification": registration_options.authenticator_selection.user_verification.value,
        },
    }

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Register Passkey</title>
        <script src="https://cdn.jsdelivr.net/npm/@simplewebauthn/browser@7/dist/bundle/starter.js"></script>
    </head>
    <body>
        <h1>Register Passkey</h1>
        <button id="register-button">Register Passkey</button>
        <script>
            const registerButton = document.getElementById('register-button');
            registerButton.addEventListener('click', async () => {{
                try {{
                    const options = {json.dumps(serialized_options)};
                    const credential = await SimpleWebAuthnBrowser.create(options);
                    
                    const response = await fetch('/verify_passkey', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{
                            login_id: '{login_id}',
                            credential: credential
                        }})
                    }});
                    
                    const result = await response.json();
                    if (result.success) {{
                        alert('Passkey registered successfully! Please return to your WhatsApp conversation.');
                    }} else {{
                        alert('Registration failed: ' + result.error);
                    }}
                }} catch (error) {{
                    console.error('Registration error:', error);
                    alert('Registration failed. Please try again.');
                }}
            }});
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.post("/verify_passkey")
async def verify_passkey(request: Request):
    data = await request.json()
    login_id = data.get("login_id")
    credential = data.get("credential")

    if login_id not in login_requests:
        return JSONResponse({"error": "Invalid registration session"}, status_code=400)

    registration_data = login_requests[login_id]
    phone_number = registration_data["phone_number"]

    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=registration_data["challenge"],
            expected_origin=callback_url,
            expected_rp_id=callback_url,
        )

        users[phone_number] = {
            "user_id": bytes.fromhex(registration_data["user_id"]),
            "credential_id": verification.credential_id,
            "public_key": verification.credential_public_key,
            "last_login": time.time(),
        }

        del login_requests[login_id]

        return JSONResponse(
            {"success": True, "message": "Passkey registered successfully"}
        )
    except Exception as e:
        logger.error(f"Passkey verification failed: {str(e)}")
        return JSONResponse({"error": "Passkey verification failed"}, status_code=400)


@app.get("/login")
async def login(phone_number: str = Query(..., description="The user's phone number")):
    if phone_number not in users:
        return HTMLResponse("Error: User not registered")

    login_id = secrets.token_urlsafe(16)
    login_requests[login_id] = {"phone_number": phone_number, "timestamp": time.time()}

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Passkey Login</title>
        <script src="https://cdn.jsdelivr.net/npm/@simplewebauthn/browser@7/dist/bundle/starter.js"></script>
    </head>
    <body>
        <h1>Passkey Login</h1>
        <button id="login-button">Login with Passkey</button>
        <script>
            const loginButton = document.getElementById('login-button');
            loginButton.addEventListener('click', async () => {{
                try {{
                    const credential = await SimpleWebAuthnBrowser.get({{
                        rpId: '{callback_url}',
                        challenge: '{login_id}',
                    }});
                    
                    const response = await fetch('/verify_login', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{
                            login_id: '{login_id}',
                            credential: credential
                        }})
                    }});
                    
                    const result = await response.json();
                    if (result.success) {{
                        alert('Login successful! Please return to your WhatsApp conversation.');
                    }} else {{
                        alert('Login failed: ' + result.error);
                    }}
                }} catch (error) {{
                    console.error('Login error:', error);
                    alert('Login failed. Please try again.');
                }}
            }});
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.post("/verify_login")
async def verify_login(request: Request):
    data = await request.json()
    login_id = data.get("login_id")
    credential = data.get("credential")

    if login_id not in login_requests:
        return JSONResponse({"error": "Invalid login session"}, status_code=400)

    login_data = login_requests[login_id]
    phone_number = login_data["phone_number"]
    user_data = users[phone_number]

    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=login_id,
            expected_origin=callback_url,
            expected_rp_id=callback_url,
        )

        users[phone_number]["last_login"] = time.time()
        del login_requests[login_id]

        wa.send_message(to=phone_number, text="You've successfully logged in!")

        return JSONResponse({"success": True})
    except Exception as e:
        logger.error(f"Login verification failed: {str(e)}")
        return JSONResponse({"error": "Login verification failed"}, status_code=400)


@wa.on_message()
async def handle_message(client: WhatsApp, msg: Message):
    phone_number = msg.from_user.wa_id

    if phone_number not in users:
        registration_link = (
            f"{callback_url}/generate_passkey?phone_number={phone_number}"
        )
        client.send_message(
            to=phone_number,
            text=f"Please register your passkey first: {registration_link}",
        )
    elif time.time() - users[phone_number]["last_login"] > 24 * 60 * 60:  # 24 hours
        login_link = f"{callback_url}/login?phone_number={phone_number}"
        client.send_message(to=phone_number, text=f"Please log in again: {login_link}")
    else:
        # Process authenticated message
        response = f"Authenticated user message: {msg.text}"
        client.send_message(to=phone_number, text=response)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
