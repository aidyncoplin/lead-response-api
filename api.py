import os
import csv
import smtplib
from email.message import EmailMessage

from dotenv import load_dotenv
from openai import OpenAI
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="Lead Response API")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


class Lead(BaseModel):
    name: str
    service: str
    interest: str
    notify_email: str


def generate_followup(name: str, service: str, interest: str) -> str:
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You write short, professional follow-up messages for small businesses."},
            {"role": "user", "content": f"Name: {name}\nService: {service}\nInterest: {interest}\nWrite a 2-sentence text message follow-up."},
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

    msg = generate_followup(lead.name, lead.service, lead.interest)
    save_to_csv(lead.name, lead.service, lead.interest, msg)

    try:
        send_email(
            to_email=lead.notify_email,
            subject=f"New lead follow-up generated for {lead.name}",
            body=msg,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Email send failed: {type(e).__name__}: {e}")

    return {"reply": msg}