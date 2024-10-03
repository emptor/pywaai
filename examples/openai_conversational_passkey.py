import os
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pywa import WhatsApp
from pywa.types import Message
from pydantic import BaseModel
from typing import Dict, Any
from loguru import logger
import secrets
import time
from webauthn import (
    generate_registration_options,
    verify_registration_response,
    generate_authentication_options,
    verify_authentication_response,
    options_to_json,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    UserVerificationRequirement,
    PublicKeyCredentialDescriptor,
    AttestationConveyancePreference,
)

# TODO Improve UX for registration and login, unsure why it works in some cases and not others

# Environment variables
WA_TOKEN = os.environ.get("WHATSAPP_MANAGER_TOKEN")

# Initialize FastAPI app
app = FastAPI()

# WhatsApp configuration
AGENT_PHONE_NUMBER = "51922776803"
phone_id = "392248423969335"
app_id = 1655952435197468
app_secret = "9bfe44b4a12ba3f793282a6136203eea"
verify_token = "ABD361"
callback_url = "whatsapp.emptor-cdn.com"  # Remove https:// from the callback_url
business_account_id = "391057337423244"
verify_timeout = 10

# Initialize WhatsApp client
wa = WhatsApp(
    token=WA_TOKEN,
    phone_id=phone_id,
    app_id=app_id,
    app_secret=app_secret,
    server=app,
    verify_token=verify_token,
    callback_url=f"https://{callback_url}",  # Add https:// here
    business_account_id=business_account_id,
    verify_timeout=verify_timeout,
)

# Store user credentials and login information
user_credentials: Dict[str, Dict[str, Any]] = {}
login_requests: Dict[str, Dict[str, Any]] = {}
last_login_time: Dict[str, float] = {}


class PasskeyRegistrationResponse(BaseModel):
    phone_number: str
    credential: Dict[str, Any]


@app.get("/register")
async def register(
    phone_number: str = Query(..., description="The user's phone number")
):
    user_id = secrets.token_bytes(32)
    login_id = secrets.token_urlsafe(16)

    registration_options = generate_registration_options(
        rp_id=callback_url,  # Use the domain without https://
        rp_name="WhatsApp Chatbot",
        user_id=user_id,
        user_name=phone_number,
        user_display_name=phone_number,
        attestation=AttestationConveyancePreference.DIRECT,
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.REQUIRED
        ),
        supported_pub_key_algs=[
            COSEAlgorithmIdentifier.ECDSA_SHA_256,
            COSEAlgorithmIdentifier.RSASSA_PSS_SHA_256,
        ],
    )

    login_requests[login_id] = {
        "phone_number": phone_number,
        "challenge": registration_options.challenge,
        "user_id": user_id.hex(),
        "timestamp": time.time(),
    }

    # Serialize registration options for client-side use
    serialized_options = options_to_json(registration_options)

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Register Passkey</title>
        <script src="https://cdn.jsdelivr.net/npm/@simplewebauthn/browser@7/dist/bundle/index.umd.min.js"></script>
    </head>
    <body>
        <h1>Register Your Passkey</h1>
        <button id="register">Register</button>
        <script>
            const registerButton = document.getElementById('register');
            registerButton.addEventListener('click', async () => {{
                const options = {serialized_options};
                
                try {{
                    const credential = await SimpleWebAuthnBrowser.startRegistration(options);
                    
                    const response = await fetch('/register/complete?login_id={login_id}', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify(credential)
                    }});
                    const data = await response.json();
                    if (data.status === 'success') {{
                        alert('Registration successful! You can now close this window and return to WhatsApp.');
                    }} else {{
                        alert('Registration failed. Please try again.');
                    }}
                }} catch (error) {{
                    console.error('Error:', error);
                    alert('An error occurred during registration. Please try again.');
                }}
            }});
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content, status_code=200)


@app.post("/register/complete")
async def register_complete(request: Request, login_id: str = Query(...)):
    if login_id not in login_requests:
        return JSONResponse({"status": "error", "message": "Invalid login ID"})

    registration_data = login_requests[login_id]
    phone_number = registration_data["phone_number"]

    try:
        credential = await request.json()
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=registration_data["challenge"],
            expected_origin=f"https://{callback_url}",  # Add https:// here
            expected_rp_id=callback_url,  # Use the domain without https://
        )

        user_credentials[phone_number] = {
            "public_key": verification.credential_public_key,
            "sign_count": verification.sign_count,
        }

        del login_requests[login_id]

        # Send WhatsApp message to confirm registration
        wa.send_message(
            to=phone_number, text="Your passkey has been successfully registered!"
        )

        return JSONResponse({"status": "success"})
    except Exception as e:
        logger.error(f"Error during registration: {str(e)}")
        return JSONResponse({"status": "error", "message": "Registration failed"})


@app.get("/login")
async def login(phone_number: str = Query(..., description="The user's phone number")):
    if phone_number not in user_credentials:
        return HTMLResponse("Error: User not registered")

    login_id = secrets.token_urlsafe(16)

    authentication_options = generate_authentication_options(
        rp_id=callback_url,  # Use the domain without https://
        allow_credentials=[
            PublicKeyCredentialDescriptor(
                id=user_credentials[phone_number]["public_key"]
            )
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )

    login_requests[login_id] = {
        "phone_number": phone_number,
        "challenge": authentication_options.challenge,
        "timestamp": time.time(),
    }

    serialized_options = options_to_json(authentication_options)

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Login with Passkey</title>
        <script src="https://cdn.jsdelivr.net/npm/@simplewebauthn/browser@7/dist/bundle/index.umd.min.js"></script>
    </head>
    <body>
        <h1>Login with Your Passkey</h1>
        <p>Your password manager (e.g., 1Password) should automatically offer to use your saved passkey. If not, click the button below to login.</p>
        <button id="login">Login with Passkey</button>
        <script>
            async function performAuthentication() {{
                const options = {serialized_options};
                
                try {{
                    const assertion = await SimpleWebAuthnBrowser.startAuthentication(options);
                    const response = await fetch('/login/complete?login_id={login_id}', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify(assertion)
                    }});
                    const data = await response.json();
                    if (data.status === 'success') {{
                        alert('Login successful! You can now close this window and return to WhatsApp.');
                    }} else {{
                        alert('Login failed. Please try again.');
                    }}
                }} catch (error) {{
                    console.error('Error:', error);
                    alert('An error occurred during login. Please try again.');
                }}
            }}

            const loginButton = document.getElementById('login');
            loginButton.addEventListener('click', performAuthentication);

            // Automatically trigger the login process when the page loads
            window.addEventListener('load', performAuthentication);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content, status_code=200)


@app.post("/login/complete")
async def login_complete(request: Request, login_id: str = Query(...)):
    if login_id not in login_requests:
        return JSONResponse({"status": "error", "message": "Invalid login ID"})

    login_data = login_requests[login_id]
    phone_number = login_data["phone_number"]

    try:
        credential = await request.json()
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=login_data["challenge"],
            expected_rp_id=callback_url,  # Use the domain without https://
            expected_origin=f"https://{callback_url}",  # Add https:// here
            credential_public_key=user_credentials[phone_number]["public_key"],
            credential_current_sign_count=user_credentials[phone_number]["sign_count"],
            require_user_verification=True,
        )

        user_credentials[phone_number]["sign_count"] = verification.new_sign_count
        last_login_time[phone_number] = time.time()
        del login_requests[login_id]

        # Send WhatsApp message to confirm login
        wa.send_message(to=phone_number, text="You've successfully logged in!")

        return JSONResponse({"status": "success"})
    except Exception as e:
        logger.error(f"Error during login: {str(e)}")
        return JSONResponse({"status": "error", "message": "Login failed"})


@wa.on_message()
async def handle_message(client: WhatsApp, msg: Message):
    phone_number = msg.from_user.wa_id

    if phone_number not in user_credentials:
        registration_link = (
            f"https://{callback_url}/register?phone_number={phone_number}"
        )
        client.send_message(
            to=phone_number,
            text=f"Please register your passkey first: {registration_link}",
        )
    elif (
        phone_number not in last_login_time
        or time.time() - last_login_time[phone_number] > 86400
    ):
        login_link = f"https://{callback_url}/login?phone_number={phone_number}"
        client.send_message(
            to=phone_number, text=f"Please log in with your passkey: {login_link}"
        )
    else:
        # Process authenticated message
        response = f"Authenticated user message: {msg.text}"
        client.send_message(to=phone_number, text=response)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
