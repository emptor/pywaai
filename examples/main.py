from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import webauthn

app = FastAPI()


# Dummy database to store user credentials
user_credentials = {}


@app.post("/register")
async def register(username: str = Form(...)):
    # Generate WebAuthn credential creation options
    try:
        options = webauthn.generate_registration_options(
            rp_id="localhost", user_id=username, user_name=username
        )
        user_credentials[username] = options
        return options
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/login")
async def login(
    username: str = Form(...),
    client_data: str = Form(...),
    attestation_object: str = Form(...),
):
    # Verify WebAuthn credential
    try:
        credential = user_credentials.get(username)
        if not credential:
            raise HTTPException(status_code=404, detail="User not found")
        webauthn.verify_authentication_response(
            credential=credential,
            credential_id=credential.credential_id,
            client_data=client_data,
            auth_data=attestation_object,
            expected_challenge=credential.challenge,
        )
        return {"status": "OK"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8080)
