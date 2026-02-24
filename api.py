import os
import csv
from dotenv import load_dotenv
from openai import OpenAI
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

load_dotenv()

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
API_SECRET = os.getenv("API_SECRET")

if not OPENAI_KEY:
    raise RuntimeError("OPENAI_API_KEY not set")
if not API_SECRET:
    raise RuntimeError("API_SECRET not set")

client = OpenAI(api_key=OPENAI_KEY)
app = FastAPI(title="Lead Response API")


class Lead(BaseModel):
    name: str
    service: str
    interest: str


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


@app.get("/")
def root():
    return {"status": "ok", "message": "Lead Response API is running"}


@app.post("/generate-lead-response")
def generate_lead_response(lead: Lead, x_api_key: str = Header(default="", alias="X-API-KEY")):
    if x_api_key != API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    msg = generate_followup(lead.name, lead.service, lead.interest)
    save_to_csv(lead.name, lead.service, lead.interest, msg)
    return {"reply": msg}