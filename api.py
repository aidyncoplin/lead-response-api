import os
import csv
import json
import smtplib
from email.message import EmailMessage

from dotenv import load_dotenv
from openai import OpenAI
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from twilio.rest import Client as TwilioClient

load_dotenv()

app = FastAPI(title="Lead Response API")

# OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# -------------------------
# Data Model
# -------------------------

class Lead(BaseModel):
    name: str
    service: str
    interest: str
    notify_email: str
    lead_phone: str


# -------------------------
# Helper Functions
# -------------------------

def generate_followup(name: str, service: str, interest: str) -> str:
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "You are a professional sales assistant. Write a short, friendly SMS under 180 characters. Include the customer's name. Be conversational, not corporate. End with a question that encourages a reply."
            },
            {
                "role": "user",
                "content": f"Name: {name}\nService: {service}\nInterest: {interest}\nWrite a 2-sentence text message follow-up.",
            },
        ],
    )
    return resp.choices[0].message.content


def save_to_csv(name: str, service: str, interest: str, message: str) -> None:
    file_exists = os.path.isfile("leads.csv")

    with open("leads.csv", mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow(["Name", "Service", "Interest", "AI_Response"])

        writer.writerow([name, service, interest, message])


def send_email(to_email: str, subject: str, body: str) -> None:
    email_from = os.getenv("EMAIL_FROM")
    app_pw = os.getenv("EMAIL_APP_PASSWORD")

    if not email_from or not app_pw:
        raise RuntimeError("Missing EMAIL_FROM or EMAIL_APP_PASSWORD")

    msg = EmailMessage()
    msg["From"] = email_from
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(email_from, app_pw)
        smtp.send_message(msg)


def send_sms(to_number: str, body: str) -> None:
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_FROM_NUMBER")

    if not sid or not token or not from_number:
        raise RuntimeError("Missing Twilio env vars")

    tw = TwilioClient(sid, token)
    tw.messages.create(
        to=to_number,
        from_=from_number,
        body=body,
    )
def generate_followup_sequence(name: str, service: str, interest: str) -> dict:
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Return ONLY valid JSON (no markdown, no extra text) with keys: "
                    "msg_0, msg_24h, msg_72h. Each value is an SMS under 120 characters. "
                    "Friendly, casual, includes the customer's name, ends with a question."
                ),
            },
            {
                "role": "user",
                "content": f"Name: {name}\nService: {service}\nInterest: {interest}",
            },
        ],
    )

    text = resp.choices[0].message.content.strip()
    return json.loads(text)

# -------------------------
# Routes
# -------------------------

@app.get("/")
def root():
    return {"status": "ok", "message": "Lead Response API is running"}


@app.post("/generate-lead-response")
def generate_lead_response(
    lead: Lead,
    x_api_key: str = Header(default="", alias="X-API-KEY"),
):
    api_secret = os.getenv("API_SECRET")

    if not api_secret:
        raise HTTPException(status_code=500, detail="Server misconfigured: API_SECRET missing")

    if x_api_key != api_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 1) Generate AI message
    seq = generate_followup_sequence(lead.name, lead.service, lead.interest)
    msg = seq["msg_0"]

    # 2) Save it
    save_to_csv(lead.name, lead.service, lead.interest, msg)

    # 3) Send email
    try:
        send_email(
            to_email=lead.notify_email,
            subject=f"New lead follow-up generated for {lead.name}",
            body=(
            f"Immediate:\n{seq['msg_0']}\n\n"
            f"+24h:\n{seq['msg_24h']}\n\n"
            f"+72h:\n{seq['msg_72h']}\n"
        ),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Email send failed: {type(e).__name__}: {e}")

    # SMS MODE: "test" sends to TEST_SMS_TO, "live" sends to the lead's phone
    sms_mode = (os.getenv("SMS_MODE") or "test").lower()

    if sms_mode == "live":
        sms_to = lead.lead_phone
    else:
        sms_to = os.getenv("TEST_SMS_TO")

    if not sms_to:
        raise HTTPException(status_code=500, detail="SMS target missing (check SMS_MODE/TEST_SMS_TO/lead_phone)")

    try:
        send_sms(sms_to, msg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SMS send failed: {type(e).__name__}: {e}")
        

    return {
        "sequence": seq,
        "emailed_to": lead.notify_email,
        "sms_sent_to": sms_to,
        "sms_mode": sms_mode,
    }