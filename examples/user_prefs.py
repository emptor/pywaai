import flask  # pip3 install flask
from pywa import WhatsApp, filters
from pywa.types import Message, Button

flask_app = flask.Flask(__name__)
wa = WhatsApp(
    phone_id="your_phone_number",
    token="your_token",
    server=flask_app,
    verify_token="xyzxyz",
)

# A simple dictionary to store user preferences
USER_PREFS = {}

@wa.on_message(filters.command("start"))
def start(_: WhatsApp, msg: Message):
    """
    This handler will be called when user sends `/start` command.

    It will send a welcome message with two buttons for setting user preferences.
    """
    msg.reply_text(
        text="Welcome! Please select your preferred language:",
        buttons=[
            Button(title="English", callback_data="lang:en"),
            Button(title="Español", callback_data="lang:es"),
        ],
    )


@wa.on_callback_button(filters.startswith("lang:"))
def set_language(_: WhatsApp, clb: Button):
    """
    This handler will be called when user clicks on one of the language buttons.

    It will store user's preferred language and send a confirmation message.
    """
    lang_code = clb.data.split(":")[1]
    USER_PREFS[clb.from_user.wa_id] = {"language": lang_code}
    clb.reply_text(f"Your preferred language has been set to: {lang_code}")


@wa.on_message(filters.text)
def echo(_: WhatsApp, msg: Message):
    """
    This handler will be called when user sends any text message.

    It will echo the message back to the user, respecting their preferred language if set.
    """
    user_id = msg.from_user.wa_id
    language = USER_PREFS.get(user_id, {}).get("language")

    if language == "es":
        reply_text = f"¡Dijiste: '{msg.text}'!"
    else:
        reply_text = f"You said: '{msg.text}'"

    msg.reply_text(reply_text)


# Run the server
flask_app.run()
