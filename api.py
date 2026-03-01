import os
import json
import uuid
import re
import datetime
import sqlite3
import smtplib
from email.message import EmailMessage

from dotenv import load_dotenv
from openai import OpenAI

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Dial

load_dotenv()

app = FastAPI(title="Lead Response API")

# -------------------------
# Config
# -------------------------
DB_PATH = os.getenv("DB_PATH") or "app.db"

API_SECRET = os.getenv("API_SECRET") or ""
DISPATCH_SECRET = os.getenv("DISPATCH_SECRET") or ""
DEMO_KEY = os.getenv("DEMO_KEY") or ""

SMS_MODE = (os.getenv("SMS_MODE") or "test").lower()  # "test" or "live"
TEST_SMS_TO = os.getenv("TEST_SMS_TO") or ""

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL") or ""  # e.g. https://lead-response-api.onrender.com
ROOFER_FORWARD_TO = os.getenv("ROOFER_FORWARD_TO") or ""  # the roofer's real phone number
DEFAULT_NOTIFY_EMAIL = os.getenv("DEFAULT_NOTIFY_EMAIL") or ""  # used for voice missed-call leads

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
# DB helpers
# -------------------------
def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def ensure_column(con, table: str, col: str, ddl: str):
    """
    ddl example: "ALTER TABLE leads ADD COLUMN responded INTEGER NOT NULL DEFAULT 0"
    """
    cur = con.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in cur.fetchall()]
    if col not in cols:
        cur.execute(ddl)

def init_db():
    con = db_conn()
    cur = con.cursor()

    # leads table (keeps your existing structure but adds responded)
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
            sms_mode TEXT NOT NULL,
            responded INTEGER NOT NULL DEFAULT 0
        )
    """)

    # followup jobs (adds canceled as a possible status)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS followup_jobs (
            id TEXT PRIMARY KEY,
            lead_id TEXT NOT NULL,
            run_at_utc TEXT NOT NULL,
            to_number TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL,   -- pending|sent|failed|canceled
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_utc TEXT NOT NULL
        )
    """)

    # If DB existed before, ensure responded column exists
    ensure_column(
        con,
        table="leads",
        col="responded",
        ddl="ALTER TABLE leads ADD COLUMN responded INTEGER NOT NULL DEFAULT 0"
    )

    con.commit()
    con.close()

init_db()

# -------------------------
# Validation helpers
# -------------------------
def is_valid_e164(phone: str) -> bool:
    return bool(re.fullmatch(r"\+\d{10,15}", phone or ""))

def is_valid_email(email: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or ""))

def extract_first_name(text: str) -> str:
    """
    Pull a likely first name from common reply patterns.
    Returns "" if none found.
    """
    if not text:
        return ""
    t = text.strip()

    patterns = [
        r"\b(?:i'?m|im|i am|this is)\s+([A-Za-z]{2,20})\b",
        r"\b([A-Za-z]{2,20})\s+(?:here)\b",
        r"^([A-Za-z]{2,20})\b",  # starts with a name
    ]

    for p in patterns:
        m = re.search(p, t, flags=re.IGNORECASE)
        if m:
            name = m.group(1)
            # normalize capitalization
            name = name[:1].upper() + name[1:].lower()
            # reject common non-names
            if name.lower() in {"yes", "yeah", "yep", "ok", "okay", "call", "sure"}:
                return ""
            return name
    return ""

def update_lead_name_by_phone(phone: str, first_name: str) -> None:
    if not first_name:
        return
    con = db_conn()
    cur = con.cursor()
    # Only update if the saved name looks like unknown/blank
    cur.execute("""
        UPDATE leads
        SET name = ?
        WHERE lead_phone = ?
          AND (name IS NULL OR name='' OR name='(unknown)')
    """, (first_name, phone))
    con.commit()
    con.close()

def require_api_key(x_api_key: str):
    if not API_SECRET:
        raise HTTPException(status_code=500, detail="Server misconfigured: API_SECRET missing")
    if x_api_key != API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

def require_dispatch_key(x_dispatch_key: str):
    if not DISPATCH_SECRET:
        raise HTTPException(status_code=500, detail="Server misconfigured: DISPATCH_SECRET missing")
    if x_dispatch_key != DISPATCH_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

def require_demo_key(x_demo_key: str):
    # If DEMO_KEY is blank, demo is public
    if DEMO_KEY and x_demo_key != DEMO_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized demo")

# -------------------------
# Email + SMS
# -------------------------
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

def pick_sms_target(lead_phone: str) -> str:
    if SMS_MODE == "live":
        return lead_phone
    if not TEST_SMS_TO:
        raise RuntimeError("SMS_MODE is test but TEST_SMS_TO is missing")
    return TEST_SMS_TO

# -------------------------
# OpenAI message generation
# -------------------------
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
                    "Never imply the roof was already inspected, reviewed, or assessed.\n\n"

                    "Style rules:\n"
                    "- msg_0 MUST end with a scheduling question (two options).\n"
                    "- Avoid phrases like 'we need to', 'just checking in', 'time is crucial'.\n"
                    "- Prefer 'roof check' or 'inspection' instead of 'estimate' in msg_0.\n"
                    "- Sound busy and in demand, not desperate.\n\n"

                    "Strong tone examples (match this structure and tone):\n"
                    "msg_0: Mike — saw the storm roll through your area. We’re doing roof checks this week. Today or tomorrow?\n"
                    "msg_24h: Quick follow up, Mike. Adjusters are filling up. Want us to check it Thursday or Friday?\n"
                    "msg_72h: Last note, Mike — small storm damage turns costly fast. Still want a roof check this week?\n"
                ),
            },
            {
                "role": "user",
                "content": f"Name: {name}\nService: {service}\nInterest: {interest}",
            },
        ],
    )

    text = (resp.choices[0].message.content or "").strip()
    try:
        data = json.loads(text)
        # Hard safety fallback if keys missing
        return {
            "msg_0": str(data.get("msg_0", "")).strip()[:120],
            "msg_24h": str(data.get("msg_24h", "")).strip()[:120],
            "msg_72h": str(data.get("msg_72h", "")).strip()[:120],
        }
    except json.JSONDecodeError:
        return {
            "msg_0": "Sorry — quick roof check this week. Today or tomorrow?",
            "msg_24h": "Adjusters are filling up. Want us to check it Thursday or Friday?",
            "msg_72h": "Small storm damage turns costly fast. Still want a roof check this week?",
        }

# -------------------------
# DB writes
# -------------------------
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
    sms_mode: str,
) -> None:
    con = db_conn()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO leads
        (id, created_utc, name, service, interest, lead_phone, notify_email,
         msg_0, msg_24h, msg_72h, sms_target, sms_mode, responded)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
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
        (base_time_utc + datetime.timedelta(hours=24), seq["msg_24h"]),
        (base_time_utc + datetime.timedelta(hours=72), seq["msg_72h"]),
    ]

    created_utc = datetime.datetime.utcnow().isoformat()

    for run_at, body in jobs:
        job_id = str(uuid.uuid4())
        cur.execute("""
            INSERT INTO followup_jobs
            (id, lead_id, run_at_utc, to_number, body, status, attempts, last_error, created_utc)
            VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, ?)
        """, (job_id, lead_id, run_at.isoformat(), to_number, body, created_utc))

    con.commit()
    con.close()

def mark_responded_by_phone(phone: str) -> int:
    """
    Set responded=1 for leads matching this phone.
    Also cancel any pending followup_jobs for those leads.
    Returns how many leads were updated.
    """
    con = db_conn()
    cur = con.cursor()

    cur.execute("SELECT id FROM leads WHERE lead_phone = ? AND responded = 0", (phone,))
    lead_ids = [row[0] for row in cur.fetchall()]

    cur.execute("UPDATE leads SET responded = 1 WHERE lead_phone = ?", (phone,))

    if lead_ids:
        cur.execute(
            f"UPDATE followup_jobs SET status='canceled' WHERE status='pending' AND lead_id IN ({','.join(['?']*len(lead_ids))})",
            tuple(lead_ids),
        )

    con.commit()
    con.close()
    return len(lead_ids)

# -------------------------
# Routes
# -------------------------
@app.get("/")
def root():
    return {"status": "ok", "message": "Lead Response API is running"}

@app.get("/demo")
def demo():
    return {
        "headline": "When You Miss a Storm Call, This Is What The Homeowner Sees",
        "problem": "During hail or storm spikes, call volume overwhelms staff. Some calls go unanswered.",
        "solution": "Instead of voicemail, the homeowner instantly receives:",
        "lead_example": {"name": "Mike", "service": "Roofing estimate", "interest": "Storm damage"},
        "sequence_preview": {
            "Immediate (sent within seconds of missed call)":
                "Mike — saw the storm roll through your area. We’re doing roof checks this week. Today or tomorrow?",
            "24 Hours Later (if no reply)":
                "Quick follow up, Mike. Adjusters are filling up. Want us to check it Thursday or Friday?",
            "72 Hours Later (final touch)":
                "Last note, Mike — small storm damage turns costly fast. Still want a roof check this week?",
        },
        "impact": "If even 1 missed storm call converts, that can mean $10,000–$25,000 recovered revenue."
    }

@app.post("/demo-generate")
def demo_generate(lead: Lead):
    # Demo only (no auth, no SMS, no email, no DB)
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
    .sub { color: #444; margin-bottom: 18px; }
    .context { background:#f6f7f9; border:1px solid #e2e5ea; padding:12px; border-radius:10px; margin-bottom: 18px;}
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
  <h1>When You Miss a Storm Call, This Is What The Homeowner Sees</h1>
  <div class="sub">Simulate a missed call below. Generate the exact SMS follow-ups your system would send.</div>

  <div class="context">
    <b>How it works:</b> Call goes unanswered → homeowner gets an immediate text → 24h + 72h follow-ups if no reply.
  </div>

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

# -------------------------
# Followup dispatcher (cron calls this)
# IMPORTANT: Must be POST (cron-job.org was hitting GET before → 405)
# -------------------------
@app.post("/dispatch-followups")
def dispatch_followups(x_dispatch_key: str = Header(default="", alias="X-DISPATCH-KEY")):
    require_dispatch_key(x_dispatch_key)

    now = datetime.datetime.utcnow().isoformat()

    con = db_conn()
    cur = con.cursor()

    # Only send jobs for leads that have NOT responded
    cur.execute("""
        SELECT j.id, j.to_number, j.body, j.attempts
        FROM followup_jobs j
        JOIN leads l ON l.id = j.lead_id
        WHERE j.status='pending'
          AND l.responded = 0
          AND j.run_at_utc <= ?
        ORDER BY j.run_at_utc ASC
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

# -------------------------
# Twilio inbound SMS: STOP followups after reply
# Set Twilio Messaging webhook to POST /twilio/sms
# -------------------------
@app.post("/twilio/sms")
async def twilio_inbound_sms(request: Request):
    form = await request.form()
    from_number = form.get("From")  # lead phone in E.164
    body = (form.get("Body") or "").strip()

    if from_number and is_valid_e164(from_number):
        updated = mark_responded_by_phone(from_number)
        return {"status": "received", "from": from_number, "updated_leads": updated, "body_preview": body[:80]}

    return {"status": "ignored"}

# -------------------------
# Twilio voice: forward call to roofer, if missed → auto-text + schedule followups
# Set Twilio Voice webhook to POST /twilio/voice
# -------------------------
@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    form = await request.form()
    caller = form.get("From")  # lead's number
    vr = VoiceResponse()

    if not PUBLIC_BASE_URL or not ROOFER_FORWARD_TO:
        vr.say("System not configured.")
        vr.hangup()
        return Response(content=str(vr), media_type="text/xml")

    dial = Dial(
        timeout=20,
        action=f"{PUBLIC_BASE_URL}/twilio/dial-status?caller={caller}",
        method="POST",
    )
    dial.number(ROOFER_FORWARD_TO)
    vr.append(dial)
    return Response(content=str(vr), media_type="text/xml")

@app.post("/twilio/dial-status")
async def twilio_dial_status(request: Request):
    form = await request.form()
    status = (form.get("DialCallStatus") or "").lower()
    caller = request.query_params.get("caller") or form.get("From")

    # Only if not answered
    if status in ("no-answer", "busy", "failed", "canceled") and caller and is_valid_e164(caller):
        # Generate generic messages (we don't know name/service yet)
        seq = generate_followup_sequence(
            name="there",
            service="Roof check",
            interest="Storm damage",
        )
        sms_to = pick_sms_target(caller)
        created_dt = datetime.datetime.utcnow()
        lead_id = str(uuid.uuid4())
        created_utc = created_dt.isoformat()
        notify = DEFAULT_NOTIFY_EMAIL or "demo@example.com"

        # Save + send + enqueue
        try:
            save_lead_to_db(
                lead_id=lead_id,
                created_utc=created_utc,
                name="(unknown)",
                service="Missed call",
                interest="Storm damage",
                lead_phone=caller,
                notify_email=notify,
                seq=seq,
                sms_target=sms_to,
                sms_mode=SMS_MODE,
            )
            send_sms(sms_to, seq["msg_0"])
            enqueue_followups(lead_id=lead_id, base_time_utc=created_dt, to_number=sms_to, seq=seq)
        except Exception:
            # Never crash Twilio webhook
            pass

    vr = VoiceResponse()
    vr.hangup()
    return Response(content=str(vr), media_type="text/xml")

# -------------------------
# Main API endpoint (manual test + email + sms + followups)
# -------------------------
@app.post("/generate-lead-response")
def generate_lead_response(
    lead: Lead,
    x_api_key: str = Header(default="", alias="X-API-KEY"),
):
    require_api_key(x_api_key)

    if not lead.name.strip() or not lead.service.strip() or not lead.interest.strip():
        raise HTTPException(status_code=422, detail="name/service/interest cannot be empty")

    if not is_valid_email(lead.notify_email):
        raise HTTPException(status_code=422, detail="notify_email is invalid")

    if not is_valid_e164(lead.lead_phone):
        raise HTTPException(status_code=422, detail="lead_phone must be E.164 format like +18015551234")

    lead_id = str(uuid.uuid4())
    created_dt = datetime.datetime.utcnow()
    created_utc = created_dt.isoformat()

    seq = generate_followup_sequence(lead.name, lead.service, lead.interest)
    sms_to = pick_sms_target(lead.lead_phone)

    # Save to DB
    save_lead_to_db(
        lead_id=lead_id,
        created_utc=created_utc,
        name=lead.name,
        service=lead.service,
        interest=lead.interest,
        lead_phone=lead.lead_phone,
        notify_email=lead.notify_email,
        seq=seq,
        sms_target=sms_to,
        sms_mode=SMS_MODE,
    )

    # Email owner/admin (or you for now)
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

    # Send immediate SMS and enqueue followups
    try:
        send_sms(sms_to, seq["msg_0"])
        enqueue_followups(lead_id=lead_id, base_time_utc=created_dt, to_number=sms_to, seq=seq)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SMS send failed: {type(e).__name__}: {e}")

    return {
        "lead_id": lead_id,
        "timestamp_utc": created_utc,
        "sequence": seq,
        "emailed_to": lead.notify_email,
        "sms_sent_to": sms_to,
        "sms_mode": SMS_MODE,
    }