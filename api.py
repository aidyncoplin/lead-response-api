import os
import csv
import json
import uuid
from fastapi import Request, Response
from twilio.twiml.voice_response import VoiceResponse, Dial
import re
import datetime
import smtplib
from email.message import EmailMessage

from dotenv import load_dotenv
from openai import OpenAI
from fastapi.responses import HTMLResponse
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

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        resp2 = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Return ONLY valid JSON (no markdown, no extra text). "
                        "Keys: msg_0, msg_24h, msg_72h. Values under 120 characters."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Name: {name}\nService: {service}\nInterest: {interest}",
                },
            ],
        )
        text2 = resp2.choices[0].message.content.strip()
        return json.loads(text2)

def is_valid_e164(phone: str) -> bool:
    # E.164 format: + followed by 10–15 digits
    return bool(re.fullmatch(r"\+\d{10,15}", phone or ""))


def is_valid_email(email: str) -> bool:
    # Simple sanity check (not perfect, but good enough)
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or ""))

# -------------------------
# Routes
# -------------------------

@app.get("/")
def root():
    return {"status": "ok", "message": "Lead Response API is running"}

@app.get("/demo")
def demo():
    # Fake lead data for demos
    name = "Mike"
    service = "Roofing estimate"
    interest = "Storm damage"

    seq = generate_followup_sequence(name, service, interest)

    # Also show what would be sent immediately
    return {
        "demo": True,
        "lead": {"name": name, "service": service, "interest": interest},
        "sequence": seq,
        "immediate_sms_preview": seq["msg_0"],
    }

@app.post("/demo-generate")
def demo_generate(lead: Lead):
    # No auth, no SMS, no email. Demo only.
    seq = generate_followup_sequence(lead.name, lead.service, lead.interest)
    return {"sequence": seq}

@app.get("/demo-ui", response_class=HTMLResponse)
def demo_ui():
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Storm Lead Follow-Up Demo</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 760px; margin: 40px auto; padding: 0 16px; }
    h1 { margin-bottom: 8px; }
    .sub { color: #444; margin-bottom: 24px; }
    label { display: block; margin-top: 12px; font-weight: 600; }
    input { width: 100%; padding: 10px; margin-top: 6px; font-size: 16px; }
    button { margin-top: 16px; padding: 12px 16px; font-size: 16px; cursor: pointer; }
    .box { margin-top: 18px; padding: 12px; border: 1px solid #ddd; border-radius: 8px; background: #fafafa; }
    .title { font-weight: 700; margin-bottom: 6px; }
    pre { white-space: pre-wrap; word-wrap: break-word; margin: 0; font-size: 15px; }
    .err { color: #b00020; font-weight: 600; }
  </style>
</head>
<body>
  <h1>Storm Lead Follow-Up Demo</h1>
  <div class="sub">Type a lead. Click Generate. See the exact texts your system would send.</div>

  <label>Name</label>
  <input id="name" value="Mike" />

  <label>Service</label>
  <input id="service" value="Roofing estimate" />

  <label>Interest</label>
  <input id="interest" value="Storm damage" />

  <button id="btn">Generate Messages</button>

  <div id="status" class="box" style="display:none;"></div>

  <div id="out" style="display:none;">
    <div class="box"><div class="title">Immediate (msg_0)</div><pre id="m0"></pre></div>
    <div class="box"><div class="title">+24 hours (msg_24h)</div><pre id="m24"></pre></div>
    <div class="box"><div class="title">+72 hours (msg_72h)</div><pre id="m72"></pre></div>
  </div>

<script>
const btn = document.getElementById("btn");
const statusBox = document.getElementById("status");
const out = document.getElementById("out");

function showStatus(text, isError=false) {
  statusBox.style.display = "block";
  statusBox.innerHTML = isError ? `<div class="err">${text}</div>` : text;
}

btn.addEventListener("click", async () => {
  const name = document.getElementById("name").value.trim();
  const service = document.getElementById("service").value.trim();
  const interest = document.getElementById("interest").value.trim();

  showStatus("Generating…");
  out.style.display = "none";

  try {
    const res = await fetch("/demo-generate", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        name,
        service,
        interest,
        notify_email: "demo@example.com",
        lead_phone: "+10000000000"
      })
    });

    const data = await res.json();

    if (!res.ok) {
      showStatus("Error: " + (data.detail || JSON.stringify(data)), true);
      return;
    }

    const seq = data.sequence || {};
    document.getElementById("m0").textContent = seq.msg_0 || "(missing)";
    document.getElementById("m24").textContent = seq.msg_24h || "(missing)";
    document.getElementById("m72").textContent = seq.msg_72h || "(missing)";

    statusBox.style.display = "none";
    out.style.display = "block";
  } catch (e) {
    showStatus("Error: " + e.message, true);
  }
});
</script>

</body>
</html>
"""

@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    form = await request.form()
    caller = form.get("From")  # the lead's phone number like +1...
    base = os.getenv("PUBLIC_BASE_URL")
    forward_to = os.getenv("ROOFER_FORWARD_TO")

    if not base or not forward_to:
        # If misconfigured, just hang up cleanly
        vr = VoiceResponse()
        vr.hangup()
        return Response(content=str(vr), media_type="text/xml")

    vr = VoiceResponse()

    dial = Dial(
        timeout=20,  # seconds it rings the roofer
        action=f"{base}/twilio/dial-status?caller={caller}",
        method="POST",
    )
    dial.number(forward_to)
    vr.append(dial)

    # If the dial doesn't connect, Twilio will hit /twilio/dial-status (action) next.
    return Response(content=str(vr), media_type="text/xml")


@app.post("/twilio/dial-status")
async def twilio_dial_status(request: Request):
    form = await request.form()

    # Twilio sends DialCallStatus: completed | no-answer | busy | failed | canceled
    status = form.get("DialCallStatus", "")
    caller = request.query_params.get("caller") or form.get("From")

    # Only auto-text if the roofer didn't answer
    if status in ("no-answer", "busy", "failed", "canceled") and caller:
        # Create the sequence based on "storm damage" default.
        # Later we can infer storm type by ZIP, etc.
        seq = generate_followup_sequence(
            name="there",
            service="Roof inspection",
            interest="Storm damage",
        )
        first_msg = seq["msg_0"]

        # Use your existing SMS_MODE toggle
        sms_mode = (os.getenv("SMS_MODE") or "test").lower()
        sms_to = caller if sms_mode == "live" else os.getenv("TEST_SMS_TO")

        try:
            send_sms(sms_to, first_msg)
        except Exception as e:
            # Don't crash Twilio webhook; just return OK
            pass

    vr = VoiceResponse()
    vr.hangup()
    return Response(content=str(vr), media_type="text/xml")

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

    request_id = str(uuid.uuid4())
    timestamp = datetime.datetime.utcnow().isoformat()

    if not lead.name.strip() or not lead.service.strip() or not lead.interest.strip():
        raise HTTPException(status_code=422, detail="name/service/interest cannot be empty")

    if not is_valid_email(lead.notify_email):
        raise HTTPException(status_code=422, detail="notify_email is invalid")

    if not is_valid_e164(lead.lead_phone):
        raise HTTPException(status_code=422, detail="lead_phone must be in E.164 format like +18015551234")

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
        
    print(f"[{timestamp}] Request {request_id} | Lead: {lead.name} | SMS_MODE: {sms_mode}")

    return {
        "request_id": request_id,
        "timestamp_utc": timestamp,
        "sequence": seq,
        "emailed_to": lead.notify_email,
        "sms_sent_to": sms_to,
        "sms_mode": sms_mode,
    }