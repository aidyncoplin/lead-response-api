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
init_db()

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

import sqlite3

DB_PATH = os.getenv("DB_PATH") or "app.db"

def db_conn():
    # check_same_thread=False is fine for simple FastAPI usage
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    con = db_conn()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id TEXT PRIMARY KEY,
            created_utc TEXT NOT NULL,
            name TEXT NOT NULL,
            service TEXT NOT NULL,
            interest TEXT NOT NULL,
            lead_phone TEXT NOT NULL,
            notify_email TEXT NOT NULL,
            msg_0 TEXT NOT NULL,
            msg_24h TEXT NOT NULL,
            msg_72h TEXT NOT NULL,
            sms_target TEXT NOT NULL,
            sms_mode TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS followup_jobs (
            id TEXT PRIMARY KEY,
            lead_id TEXT NOT NULL,
            run_at_utc TEXT NOT NULL,
            to_number TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL,   -- pending|sent|failed
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_utc TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()

def require_demo_key(x_demo_key: str):
    demo_key = os.getenv("DEMO_KEY") or ""
    if demo_key and x_demo_key != demo_key:
        raise HTTPException(status_code=401, detail="Unauthorized demo")

def save_lead_to_db(
    lead_id: str,
    created_utc: str,
    name: str,
    service: str,
    interest: str,
    lead_phone: str,
    notify_email: str,
    seq: dict,
    sms_target: str,
    sms_mode: str
) -> None:
    con = db_conn()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO leads
        (id, created_utc, name, service, interest, lead_phone, notify_email,
         msg_0, msg_24h, msg_72h, sms_target, sms_mode)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        lead_id, created_utc, name, service, interest, lead_phone, notify_email,
        seq["msg_0"], seq["msg_24h"], seq["msg_72h"], sms_target, sms_mode
    ))
    con.commit()
    con.close()

def enqueue_followups(lead_id: str, base_time_utc: datetime.datetime, to_number: str, seq: dict) -> None:
    con = db_conn()
    cur = con.cursor()

    jobs = [
        ("24h", base_time_utc + datetime.timedelta(minutes=1), seq["msg_24h"]),
        ("72h", base_time_utc + datetime.timedelta(minutes=2), seq["msg_72h"]),
    ]

    now_utc = datetime.datetime.utcnow().isoformat()

    for label, run_at, body in jobs:
        job_id = str(uuid.uuid4())
        cur.execute("""
            INSERT INTO followup_jobs
            (id, lead_id, run_at_utc, to_number, body, status, attempts, last_error, created_utc)
            VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, ?)
        """, (job_id, lead_id, run_at.isoformat(), to_number, body, now_utc))

    con.commit()
    con.close()

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
        "Return ONLY valid JSON (no markdown, no extra text). "
        "Keys: msg_0, msg_24h, msg_72h. "
        "Each value must be under 120 characters.\n\n"

        "Tone: experienced local storm roofing contractor texting a homeowner. "
        "Short sentences. Direct. Calm authority. No fluff. "
        "No emojis. No exclamation points. "
        "Never imply the roof was already inspected.\n\n"

        "Style rules:\n"
        "- msg_0 MUST end with a scheduling question (two options).\n"
        "- Avoid phrases like 'we need to', 'just checking in', 'time is crucial'.\n"
        "- Prefer 'roof check' or 'inspection' instead of 'estimate' in msg_0.\n"
        "- Sound busy and in demand, not desperate.\n\n"

        "Strong tone examples (match this structure and tone):\n"
        "Example 1:\n"
        "msg_0: Mike — saw the storm roll through your area. We’re doing roof checks this week. Today or tomorrow?\n"
        "msg_24h: Quick follow up, Mike. Adjusters are filling up. Want us to check it Thursday or Friday?\n"
        "msg_72h: Last note, Mike — small storm damage turns costly fast. Still want a roof check this week?\n\n"

        "Generate messages following this tone and structure."
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
        return {
            "msg_0": "Error generating message.",
            "msg_24h": "Error generating message.",
            "msg_72h": "Error generating message."
        }

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
def demo(x_demo_key: str = Header(default="", alias="X-DEMO-KEY")):
    require_demo_key(x_demo_key)
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

@app.post("/dispatch-followups")
def dispatch_followups(x_dispatch_key: str = Header(default="", alias="X-DISPATCH-KEY")):
    secret = os.getenv("DISPATCH_SECRET") or ""
    if not secret or x_dispatch_key != secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    now = datetime.datetime.utcnow().isoformat()

    con = db_conn()
    cur = con.cursor()

    cur.execute("""
        SELECT id, to_number, body, attempts
        FROM followup_jobs
        WHERE status='pending' AND run_at_utc <= ?
        ORDER BY run_at_utc ASC
        LIMIT 25
    """, (now,))
    rows = cur.fetchall()

    sent = 0
    failed = 0

    for job_id, to_number, body, attempts in rows:
        try:
            send_sms(to_number, body)
            cur.execute("UPDATE followup_jobs SET status='sent' WHERE id=?", (job_id,))
            sent += 1
        except Exception as e:
            failed += 1
            cur.execute("""
                UPDATE followup_jobs
                SET status='failed', attempts=?, last_error=?
                WHERE id=?
            """, (attempts + 1, f"{type(e).__name__}: {e}", job_id))

    con.commit()
    con.close()

    return {"now_utc": now, "sent": sent, "failed": failed, "checked": len(rows)}

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

lead_id = str(uuid.uuid4())
created_dt = datetime.datetime.utcnow()
timestamp = created_dt.isoformat()

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
save_lead_to_db(
    lead_id=lead_id,
    created_utc=timestamp,
    name=lead.name,
    service=lead.service,
    interest=lead.interest,
    lead_phone=lead.lead_phone,
    notify_email=lead.notify_email,
    seq=seq,
    sms_target=sms_to,
    sms_mode=sms_mode
)

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
        enqueue_followups(lead_id=lead_id, base_time_utc=created_dt, to_number=sms_to, seq=seq)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SMS send failed: {type(e).__name__}: {e}")
        
    print(f"[{timestamp}] Request {request_id} | Lead: {lead.name} | SMS_MODE: {sms_mode}")

    return {
        "lead_id": lead_id,
        "timestamp_utc": timestamp,
        "sequence": seq,
        "emailed_to": lead.notify_email,
        "sms_sent_to": sms_to,
        "sms_mode": sms_mode,
    }