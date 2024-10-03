import os
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pywa import WhatsApp
from pywa.types import Message
from typing import Dict
from loguru import logger
import secrets
import stripe

# Environment variables
WA_TOKEN = os.environ.get("WHATSAPP_MANAGER_TOKEN")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")

# Initialize FastAPI app
app = FastAPI()

# Initialize Stripe
stripe.api_key = STRIPE_SECRET_KEY

# WhatsApp configuration
AGENT_PHONE_NUMBER = "51922776803"
phone_id = "392248423969335"
app_id = 1655952435197468
app_secret = "9bfe44b4a12ba3f793282a6136203eea"
verify_token = "ABD361"
callback_url = "https://whatsapp.emptor-cdn.com"
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
    callback_url=callback_url,
    business_account_id=business_account_id,
    verify_timeout=10,
)

# Store payment session information
payment_sessions: Dict[str, Dict[str, str]] = {}


@app.get("/payment")
async def payment(session_id: str = Query(..., description="The payment session ID")):
    if session_id not in payment_sessions:
        return HTMLResponse("Error: Invalid payment session ID")

    phone_number = payment_sessions[session_id]["phone_number"]
    amount = payment_sessions[session_id]["amount"]

    # Create a Stripe Checkout Session
    checkout_session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": int(float(amount) * 100),
                    "product_data": {
                        "name": "Payment",
                    },
                },
                "quantity": 1,
            }
        ],
        mode="payment",
        success_url=f"{callback_url}/payment-success?session_id={session_id}",
        cancel_url=f"{callback_url}/payment-cancel?session_id={session_id}",
        client_reference_id=phone_number,
    )

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Payment</title>
        <script src="https://js.stripe.com/v3/"></script>
    </head>
    <body>
        <h1>Complete Your Payment</h1>
        <script>
            var stripe = Stripe('{STRIPE_PUBLISHABLE_KEY}');
            stripe.redirectToCheckout({{
                sessionId: '{checkout_session.id}'
            }}).then(function (result) {{
                if (result.error) {{
                    alert(result.error.message);
                }}
            }});
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.get("/payment-success")
async def payment_success(
    session_id: str = Query(..., description="The payment session ID")
):
    if session_id not in payment_sessions:
        return HTMLResponse("Error: Invalid payment session ID")

    phone_number = payment_sessions[session_id]["phone_number"]
    transaction_id = secrets.token_hex(16)

    # Send WhatsApp message to confirm payment
    wa.send_message(
        to=phone_number,
        text=f"Your payment was successful. Transaction ID: {transaction_id}",
    )

    # Clean up the payment_sessions
    del payment_sessions[session_id]

    return HTMLResponse(
        "Payment successful! You can close this window and return to WhatsApp."
    )


@app.get("/payment-cancel")
async def payment_cancel(
    session_id: str = Query(..., description="The payment session ID")
):
    if session_id not in payment_sessions:
        return HTMLResponse("Error: Invalid payment session ID")

    phone_number = payment_sessions[session_id]["phone_number"]

    # Send WhatsApp message to inform about cancelled payment
    wa.send_message(
        to=phone_number,
        text="Your payment was cancelled. If you need assistance, please contact us.",
    )

    # Clean up the payment_sessions
    del payment_sessions[session_id]

    return HTMLResponse(
        "Payment cancelled. You can close this window and return to WhatsApp."
    )


@wa.on_message()
async def handle_message(client: WhatsApp, msg: Message):
    if msg.text and msg.text.lower().startswith("pay "):
        try:
            amount = float(msg.text.split()[1])
            if amount < 0.50:
                client.send_message(
                    to=msg.from_user.wa_id,
                    text="Sorry, the minimum payment amount is $0.50. Please try again with a higher amount.",
                )
            else:
                session_id = secrets.token_urlsafe(16)
                payment_sessions[session_id] = {
                    "phone_number": msg.from_user.wa_id,
                    "amount": str(amount),
                }
                payment_link = f"{callback_url}/payment?session_id={session_id}"
                client.send_message(
                    to=msg.from_user.wa_id,
                    text=f"Please complete your payment of ${amount:.2f} here: {payment_link}",
                )
        except ValueError:
            client.send_message(
                to=msg.from_user.wa_id,
                text="Invalid amount. Please use the format 'pay <amount>', e.g., 'pay 10.99'",
            )
    else:
        client.send_message(
            to=msg.from_user.wa_id,
            text="To make a payment, please send a message in the format 'pay <amount>', e.g., 'pay 10.99'",
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
