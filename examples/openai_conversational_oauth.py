import os
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2AuthorizationCodeBearer
from pywa import WhatsApp
from pywa.types import Message
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from pydantic import BaseModel
from typing import Dict
from loguru import logger
import secrets

# Environment variables
WA_TOKEN = os.environ.get("WHATSAPP_MANAGER_TOKEN")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = "https://whatsapp.emptor-cdn.com/oauth2callback"

# Initialize FastAPI app
app = FastAPI()


# Initialize WhatsApp client
wa = WhatsApp(
    token=WA_TOKEN,
    phone_id=phone_id,
    app_id=app_id,
    app_secret=app_secret,  # Required for validation
    server=app,
    verify_token=verify_token,
    callback_url=callback_url,  # Replace with your public callback URL
    business_account_id=business_account_id,
    verify_timeout=10,
)

# OAuth2 scheme
oauth2_scheme = OAuth2AuthorizationCodeBearer(
    authorizationUrl="https://accounts.google.com/o/oauth2/auth",
    tokenUrl="https://oauth2.googleapis.com/token",
)

# Store user tokens
user_tokens: Dict[str, str] = {}

# Store login request information
login_requests: Dict[str, Dict[str, str]] = {}

# Store login ID to phone number mapping
login_id_phones: Dict[str, str] = {}

# Google OAuth flow
flow = Flow.from_client_config(
    {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    },
    scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
    redirect_uri=REDIRECT_URI,
)


@app.get("/login")
async def login(login_id: str = Query(..., description="The login ID")):
    if login_id not in login_id_phones:
        return HTMLResponse("Error: Invalid login ID")

    phone_number = login_id_phones[login_id]
    authorization_url, _ = flow.authorization_url(prompt="consent", state=login_id)
    return HTMLResponse(
        f'<a href="{authorization_url}">Click here to log in with Google</a>'
    )


@app.get("/oauth2callback")
async def oauth2callback(request: Request):
    flow.fetch_token(authorization_response=str(request.url))
    credentials = flow.credentials

    # Get user info
    user_info_service = build("oauth2", "v2", credentials=credentials)
    user_info = user_info_service.userinfo().get().execute()
    # {
    #     'id': '123456789012345678901',
    #     'email': 'user@example.com',
    #     'verified_email': True,
    #     'picture': 'https://example.com/profile-picture.jpg',
    #     'hd': 'example.com'
    # }

    email = user_info["email"]

    # Retrieve the login ID from the state
    login_id = request.query_params.get("state")
    if login_id not in login_id_phones:
        return HTMLResponse("Error: Invalid state parameter")

    phone_number = login_id_phones[login_id]

    logger.info(f"Email: {email}, Phone: {phone_number}")

    # Store the token (in a real app, you'd want to store this securely)
    user_tokens[email] = credentials.token

    # Send WhatsApp message to confirm login
    wa.send_message(to=phone_number, text=f"You've successfully logged in with {email}")

    # Clean up the login_id_phones
    del login_id_phones[login_id]

    # Return a pretty HTML page thanking the user
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Login Successful</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            .whatsapp-button {{
                display: inline-block;
                background-color: #25D366;
                color: white;
                padding: 10px 20px;
                border-radius: 30px;
                text-decoration: none;
                font-weight: bold;
                transition: background-color 0.3s;
            }}
            .whatsapp-button:hover {{
                background-color: #128C7E;
            }}
        </style>
    </head>
    <body class="bg-light">
        <div class="container">
            <div class="row justify-content-center mt-5">
                <div class="col-md-6">
                    <div class="card shadow">
                        <div class="card-body text-center">
                            <h1 class="card-title mb-4">Thank You for Logging In!</h1>
                            <p class="card-text">Your authentication was successful.</p>
                            <p class="card-text">Please return to your WhatsApp conversation to continue.</p>
                            <a href="https://wa.me/+{AGENT_PHONE_NUMBER}" class="whatsapp-button mt-3">
                                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-whatsapp" viewBox="0 0 16 16">
                                    <path d="M13.601 2.326A7.854 7.854 0 0 0 7.994 0C3.627 0 .068 3.558.064 7.926c0 1.399.366 2.76 1.057 3.965L0 16l4.204-1.102a7.933 7.933 0 0 0 3.79.965h.004c4.368 0 7.926-3.558 7.93-7.93A7.898 7.898 0 0 0 13.6 2.326zM7.994 14.521a6.573 6.573 0 0 1-3.356-.92l-.24-.144-2.494.654.666-2.433-.156-.251a6.56 6.56 0 0 1-1.007-3.505c0-3.626 2.957-6.584 6.591-6.584a6.56 6.56 0 0 1 4.66 1.931 6.557 6.557 0 0 1 1.928 4.66c-.004 3.639-2.961 6.592-6.592 6.592zm3.615-4.934c-.197-.099-1.17-.578-1.353-.646-.182-.065-.315-.099-.445.099-.133.197-.513.646-.627.775-.114.133-.232.148-.43.05-.197-.1-.836-.308-1.592-.985-.59-.525-.985-1.175-1.103-1.372-.114-.198-.011-.304.088-.403.087-.088.197-.232.296-.346.1-.114.133-.198.198-.33.065-.134.034-.248-.015-.347-.05-.099-.445-1.076-.612-1.47-.16-.389-.323-.335-.445-.34-.114-.007-.247-.007-.38-.007a.729.729 0 0 0-.529.247c-.182.198-.691.677-.691 1.654 0 .977.71 1.916.81 2.049.098.133 1.394 2.132 3.383 2.992.47.205.84.326 1.129.418.475.152.904.129 1.246.08.38-.058 1.171-.48 1.338-.943.164-.464.164-.86.114-.943-.049-.084-.182-.133-.38-.232z"/>
                                </svg>
                                Open WhatsApp
                            </a>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content, status_code=200)


@wa.on_message()
async def handle_message(client: WhatsApp, msg: Message):
    email = next(
        (
            email
            for email, token in user_tokens.items()
            if msg.from_user.wa_id in login_id_phones.values()
        ),
        None,
    )

    if not email:
        login_id = secrets.token_urlsafe(16)
        login_id_phones[login_id] = msg.from_user.wa_id
        login_link = f"{callback_url}/login?login_id={login_id}"
        client.send_message(
            to=msg.from_user.wa_id, text=f"Please log in first: {login_link}"
        )
    else:
        # Process authenticated message
        response = f"Authenticated user message: {msg.text}"
        client.send_message(to=msg.from_user.wa_id, text=response)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
