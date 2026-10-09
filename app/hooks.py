"""Machine-to-machine endpoints.

/twilio/*  – Twilio webhooks for calls to the Twilio number (signed by Twilio).
/ast/*     – CURL() calls from the Asterisk dialplan (extensions_crm.conf). Only
             reachable from the server itself: nginx returns 404 for /ast/, and
             requests that came through nginx (X-Real-IP set) are refused here.

Incoming call flow (Admin → Settings → Incoming calls):
  caller -> Twilio number -> POST /twilio/voice
         -> <Dial><Sip> to Asterisk -> /ast/route rings every online CRM agent
         -> nobody answers in ring_timeout s -> POST /twilio/after-dial
         -> GHL's original webhook (<Redirect>) or the inbound AI agent.

Calls to numbers on other SIP trunks (Admin → SIP trunks → Numbers) reach Asterisk
directly and ask /ast/inbound which agents to ring.
"""
import os
import re
import time
from xml.sax.saxutils import escape, quoteattr

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from . import config, db, outcomes, phone, security, trunks

router = APIRouter()

# Asterisk DIALSTATUS -> CRM call status
DIAL_STATUS = {
    "ANSWER": "answered", "NOANSWER": "no-answer", "BUSY": "busy", "CANCEL": "cancelled",
    "CONGESTION": "failed", "CHANUNAVAIL": "failed", "DONTCALL": "failed", "TORTURE": "failed",
    "INVALIDARGS": "failed",
}


INBOUND_MODES = {"crm_then_ghl", "crm_then_ai", "crm_only", "ghl_only", "ai_only"}


def twiml(body):
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>', media_type="text/xml")


def find_contact(con, number):
    return con.execute("SELECT id, name, company FROM contacts WHERE phone = ? ORDER BY id LIMIT 1", (number,)).fetchone()


def clean_name(s):
    return re.sub(r'[|"<>\\\r\n]', "", s or "")[:60]


# ---------------------------------------------------------------- Twilio ----

async def twilio_params(request: Request):
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    url = config.PUBLIC_URL + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if not security.twilio_signature_ok(url, params, request.headers.get("X-Twilio-Signature", "")):
        raise HTTPException(403, "Bad Twilio signature")
    return params


def ghl_fallback(s):
    """TwiML that hands the call to GHL exactly as if GHL had received it."""
    if s.get("ghl_voice_url"):
        method = "GET" if s.get("ghl_voice_method", "POST").upper() == "GET" else "POST"
        return f'<Redirect method="{method}">{escape(s["ghl_voice_url"])}</Redirect>'
    return ('<Say>Sorry, nobody is available to take your call. Please try again later.</Say><Hangup/>')


@router.post("/twilio/voice")
async def twilio_voice(request: Request):
    p = await twilio_params(request)
    with db.tx() as con:
        s = db.get_settings(con)
        number = phone.normalize(p.get("From", ""), s["default_country"]) or p.get("From", "unknown")
        c = find_contact(con, number)
        cur = con.execute(
            "INSERT INTO calls(direction, number, contact_id, status, twilio_sid) VALUES ('in', ?, ?, 'ringing', ?)",
            (number, c["id"] if c else None, p.get("CallSid", "")),
        )
        call_id = cur.lastrowid
        if s["inbound_mode"] == "ghl_only":
            con.execute("UPDATE calls SET status='sent-to-ghl', ended_at=? WHERE id=?", (int(time.time()), call_id))
            return twiml(ghl_fallback(s))
    if s["inbound_mode"] == "ai_only":
        return await ai_answer(call_id, p, s)
    ring = max(5, min(int(s.get("ring_timeout") or 20), 120))
    sip = f"{config.SIP_INBOUND_URI}?X-CRM-Call={call_id}"
    action = f"{config.PUBLIC_URL}/twilio/after-dial?call={call_id}"
    return twiml(
        f'<Dial timeout="{ring + 3}" answerOnBridge="true" action={quoteattr(action)} method="POST">'
        f"<Sip>{escape(sip)}</Sip></Dial>"
    )


@router.post("/twilio/after-dial")
async def twilio_after_dial(request: Request, call: int = 0):
    p = await twilio_params(request)
    status = p.get("DialCallStatus", "")
    with db.tx() as con:
        s = db.get_settings(con)
        if status in ("completed", "answered"):
            return twiml("<Hangup/>")
        if status == "canceled":          # the caller hung up while it was ringing
            con.execute("UPDATE calls SET status='missed', ended_at=? WHERE id=? AND status NOT IN ('answered')",
                        (int(time.time()), call))
            return twiml("<Hangup/>")
        if s["inbound_mode"] == "crm_then_ai" and s.get("inbound_ai_agent"):
            pass                              # handled below, outside the DB transaction
        elif s["inbound_mode"] == "crm_then_ghl" and s.get("ghl_voice_url"):
            con.execute("UPDATE calls SET status='sent-to-ghl', ended_at=? WHERE id=? AND status NOT IN ('answered')",
                        (int(time.time()), call))
            return twiml(ghl_fallback(s))
        else:
            con.execute("UPDATE calls SET status='missed', ended_at=? WHERE id=? AND status NOT IN ('answered')",
                        (int(time.time()), call))
            return twiml("<Say>Sorry, nobody is available to take your call. Please try again later.</Say><Hangup/>")
    return await ai_answer(call, p, s)


async def ai_answer(call_id, p, s):
    """Hand a Twilio call to the inbound AI agent (custom pipeline or ElevenLabs agent)."""
    from . import launcher
    from .ai import elevenlabs
    from .voice import carriers, engine
    try:
        with db.tx() as con:
            agent = launcher.load_agent(con, int(s["inbound_ai_agent"]))
            row = con.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
            c = db.row(con.execute("SELECT * FROM contacts WHERE id = ?", (row["contact_id"],)).fetchone()) \
                if row and row["contact_id"] else None
            con.execute("UPDATE calls SET ai_agent_id=?, provider='twilio', external_id=?, status='ringing', ended_at=NULL "
                        "WHERE id=?", (agent["id"], p.get("CallSid", ""), call_id))
        variables = engine.contact_vars(c or {"phone": row["number"]})
        if agent["kind"] == "custom":
            token = engine.new_token({"call_id": call_id, "agent_id": agent["id"], "vars": variables,
                                      "external_id": p.get("CallSid", ""), "from_number": p.get("To", "")})
            body = carriers.stream_twiml(token, call_id)
            return Response('<?xml version="1.0" encoding="UTF-8"?>' + body, media_type="text/xml")
        if agent["kind"] == "elevenlabs":
            xml = await elevenlabs.register_twilio_call(agent["cfg"]["el_agent_id"], p.get("From", ""),
                                                        p.get("To", ""), variables)
            return Response(xml, media_type="text/xml")
    except Exception as e:                    # never leave the caller in silence
        import logging
        logging.getLogger("crm.hooks").warning("inbound AI failed: %s", e)
    return twiml(ghl_fallback(s))


@router.post("/twilio/status")
async def twilio_status(request: Request):
    await twilio_params(request)
    return PlainTextResponse("ok")


# -------------------------------------------------------------- Asterisk ----

def ast_guard(request: Request, s: str):
    if request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for"):
        raise HTTPException(404)
    if not config.AST_SECRET or s != config.AST_SECRET:
        raise HTTPException(403)


@router.get("/ast/authorize", response_class=PlainTextResponse)
def ast_authorize(request: Request, s: str = "", ext: str = "", to: str = "", call: str = ""):
    """Outgoing call from an agent phone.
    Answer: ok|<caller id>|<call id>|<max seconds>|<record>|<trunk endpoint>|<number to dial> or deny|<reason>."""
    ast_guard(request, s)
    with db.tx() as con:
        st = db.get_settings(con)
        agent = con.execute("SELECT id, name FROM users WHERE sip_ext = ? AND active = 1", (ext,)).fetchone()
        if not agent:
            return "deny|unknown agent"
        number = phone.normalize(to, st["default_country"])
        if not number:
            return "deny|invalid number"
        reason = phone.check_allowed(number, st)
        if reason:
            return "deny|" + clean_name(reason)
        row = None
        if call.isdigit():
            row = con.execute("SELECT id, campaign_id FROM calls WHERE id = ? AND agent_id = ? AND direction = 'out' "
                              "AND status = 'new'", (int(call), agent["id"])).fetchone()
        camp = {}
        if row and row["campaign_id"]:
            r = con.execute("SELECT config FROM campaigns WHERE id = ?", (row["campaign_id"],)).fetchone()
            camp = db.jload(r["config"]) if r else {}
        trunk, caller_id = trunks.route(con, st, camp)
        if not trunk:
            return "deny|no SIP trunk configured"
        if not caller_id:
            return "deny|no caller id configured"
        if row:
            call_id = row["id"]
            con.execute("UPDATE calls SET status='dialing', number=? WHERE id=?", (number, call_id))
        else:
            c = find_contact(con, number)
            call_id = con.execute(
                "INSERT INTO calls(direction, number, contact_id, agent_id, status) VALUES ('out', ?, ?, ?, 'dialing')",
                (number, c["id"] if c else None, agent["id"]),
            ).lastrowid
    max_sec = max(60, int(float(st.get("max_call_minutes") or 60) * 60))
    rec = "1" if st.get("record_calls") == "1" else "0"
    return f"ok|{caller_id}|{call_id}|{max_sec}|{rec}|{trunks.endpoint(trunk['id'])}|{trunks.dial_number(trunk, number)}"


@router.get("/ast/route", response_class=PlainTextResponse)
def ast_route(request: Request, s: str = "", call: str = ""):
    """Incoming call from Twilio. Answer: <dial string>|<caller name>|<ring seconds> or none|."""
    ast_guard(request, s)
    now = int(time.time())
    with db.tx() as con:
        st = db.get_settings(con)
        row = con.execute("SELECT * FROM calls WHERE id = ? AND direction = 'in' AND status = 'ringing' AND started_at > ?",
                          (int(call) if call.isdigit() else 0, now - 120)).fetchone()
        if not row:      # not a call that came through our signed /twilio/voice webhook
            return "none|"
        agents = online_agents(con, now)
        if not agents:
            con.execute("UPDATE calls SET status='no-agents' WHERE id=?", (row["id"],))
            return "none|"
        c = find_contact(con, row["number"])
    name = clean_name(c["name"] if c and c["name"] else row["number"])
    ring = max(5, min(int(st.get("ring_timeout") or 20), 120))
    rec = "1" if st.get("record_calls") == "1" else "0"
    return "&".join(f"PJSIP/{a['sip_ext']}" for a in agents) + f"|{name}|{ring}|{rec}"


def online_agents(con, now):
    return con.execute(
        "SELECT sip_ext FROM users WHERE active = 1 AND available = 1 AND sip_ext IS NOT NULL AND last_seen > ?",
        (now - config.ONLINE_SECONDS,),
    ).fetchall()


@router.get("/ast/inbound", response_class=PlainTextResponse)
def ast_inbound(request: Request, s: str = "", trunk: str = "", did: str = "", to: str = "", src: str = ""):
    """Call arriving over a SIP trunk. Answer:
    <dial string>|<caller name>|<ring seconds>|<record>|<call id>|<AI if nobody answers 0/1>
    ai|<caller name>|0|<record>|<call id>|1   (the AI agent answers: dialplan -> /ast/ai-start)
    none|"""
    ast_guard(request, s)
    now = int(time.time())
    with db.tx() as con:
        st = db.get_settings(con)
        n = trunks.inbound_number(con, trunk, [did, to], st["default_country"])
        if not n or n["inbound"] not in ("agents", "agents_then_ai", "ai"):  # not one of ours, or set to reject
            return "none|"
        ai_after = n["inbound"] in ("agents_then_ai", "ai") and n["ai_agent_id"]
        number = phone.normalize(src, st["default_country"]) or clean_name(src) or "unknown"
        c = find_contact(con, number)
        call_id = con.execute("INSERT INTO calls(direction, number, contact_id, status, ai_agent_id) "
                              "VALUES ('in', ?, ?, 'ringing', ?)",
                              (number, c["id"] if c else None, n["ai_agent_id"] if ai_after else None)).lastrowid
        agents = online_agents(con, now) if n["inbound"] != "ai" else []
        if not agents and not ai_after:
            con.execute("UPDATE calls SET status='no-agents', ended_at=? WHERE id=?", (now, call_id))
            return "none|"
    name = clean_name(c["name"] if c and c["name"] else number)
    ring = max(5, min(int(st.get("ring_timeout") or 20), 120))
    rec = "1" if st.get("record_calls") == "1" else "0"
    if not agents:
        return f"ai|{name}|0|{rec}|{call_id}|1"
    return "&".join(f"PJSIP/{a['sip_ext']}" for a in agents) + f"|{name}|{ring}|{rec}|{call_id}|{1 if ai_after else 0}"


# ------------------------------------------------- AI agents on SIP trunks ----
# Dialplan context crm-ai (extensions_crm.conf) + voice/audiosocket.py

AI_FAILED = {"3": "no-answer", "5": "busy", "1": "no-answer", "8": "failed", "0": "failed"}


@router.get("/ast/ai-start", response_class=PlainTextResponse)
def ast_ai_start(request: Request, s: str = "", call: str = ""):
    """Incoming trunk call goes to its number's AI agent. Answer: ok|<audiosocket uuid> or none|."""
    import uuid
    from .voice import audiosocket, engine
    ast_guard(request, s)
    with db.tx() as con:
        row = con.execute("SELECT * FROM calls WHERE id = ? AND direction = 'in' AND ended_at IS NULL AND ai_agent_id IS NOT NULL",
                          (int(call) if call.isdigit() else 0,)).fetchone()
        if not row or not audiosocket.PORT:
            return "none|"
        a = con.execute("SELECT kind FROM ai_agents WHERE id = ?", (row["ai_agent_id"],)).fetchone()
        if not a or a["kind"] != "custom":
            return "none|"
        st = db.get_settings(con)
        t, caller = trunks.route(con, st)
        c = db.row(con.execute("SELECT * FROM contacts WHERE id = ?", (row["contact_id"],)).fetchone()) \
            if row["contact_id"] else None
        uid = str(uuid.uuid4())
        con.execute("UPDATE calls SET provider = 'sip', external_id = ? WHERE id = ?", (uid, row["id"]))
    engine.new_token({"call_id": row["id"], "agent_id": row["ai_agent_id"], "external_id": uid,
                      "vars": engine.contact_vars(c or {"phone": row["number"]}), "from_number": caller,
                      "trunk_id": t["id"] if t else None}, token=uid)
    return f"ok|{uid}"


@router.get("/ast/ai-answer", response_class=PlainTextResponse)
def ast_ai_answer(request: Request, s: str = "", call: str = ""):
    """The AI call is answered (outbound) or picked up by the AI (inbound). Answer: ok|<record 0/1> or none|."""
    ast_guard(request, s)
    with db.tx() as con:
        row = con.execute("SELECT * FROM calls WHERE id = ? AND ended_at IS NULL", (int(call) if call.isdigit() else 0,)).fetchone()
        if not row or not row["ai_agent_id"]:
            return "none|"
        con.execute("UPDATE calls SET status = 'answered' WHERE id = ?", (row["id"],))
        a = con.execute("SELECT config FROM ai_agents WHERE id = ?", (row["ai_agent_id"],)).fetchone()
    rec = "1" if a and db.jload(a["config"]).get("record") else "0"
    return f"ok|{rec}"


@router.get("/ast/ai-next", response_class=PlainTextResponse)
def ast_ai_next(request: Request, s: str = "", call: str = ""):
    """After the AI leg ended: dial|<dial string>|<caller id>, agents|<dial string>|<ring seconds> or hangup|."""
    from .voice import audiosocket
    ast_guard(request, s)
    nxt = audiosocket.NEXT.pop(int(call) if call.isdigit() else 0, None)
    if not nxt:
        return "hangup|"
    target = nxt["target"]
    with db.tx() as con:
        st = db.get_settings(con)
        if phone.E164.match(target):
            t = trunks.find(con, nxt.get("trunk_id")) or trunks.route(con, st)[0]
            if not t or phone.check_allowed(target, st):
                return "hangup|"
            return f"dial|PJSIP/{trunks.dial_number(t, target)}@{trunks.endpoint(t['id'])}|{nxt.get('caller_id', '')}"
        agents = online_agents(con, int(time.time()))      # sip:… on a SIP-trunk call = the CRM's agents
    if not agents:
        return "hangup|"
    ring = max(5, min(int(st.get("ring_timeout") or 20), 120))
    return "agents|" + "&".join(f"PJSIP/{a['sip_ext']}" for a in agents) + f"|{ring}"


@router.get("/ast/ai-failed", response_class=PlainTextResponse)
def ast_ai_failed(request: Request, s: str = "", call: str = "", reason: str = ""):
    """Outbound AI call was not answered (call file 'failed' extension; REASON 3 = no answer, 5 = busy …)."""
    from .voice import engine
    ast_guard(request, s)
    if not call.isdigit():
        return "ignored"
    for token, info in list(engine.PENDING.items()):
        if info.get("call_id") == int(call):
            engine.PENDING.pop(token, None)
    outcomes.finish_call(int(call), AI_FAILED.get(reason, "failed"), cause=f"reason {reason}"[:20])
    return "ok"


@router.get("/ast/ai-hangup", response_class=PlainTextResponse)
def ast_ai_hangup(request: Request, s: str = "", call: str = "", answered: str = ""):
    """Hangup handler of AI calls: closes calls whose AI leg never connected; attaches the recording."""
    from .voice import audiosocket
    ast_guard(request, s)
    if not call.isdigit():
        return "ignored"
    rec = os.path.join(config.RECORDINGS_DIR, f"{int(call)}.wav")
    with db.tx() as con:
        row = con.execute("SELECT status, ended_at FROM calls WHERE id = ?", (int(call),)).fetchone()
        if os.path.exists(rec):
            con.execute("UPDATE calls SET recording = ? WHERE id = ?", (rec, int(call)))
    if row and not row["ended_at"] and not audiosocket.active(int(call)):
        try:
            secs = int(float(answered or 0))
        except ValueError:
            secs = 0
        outcomes.finish_call(int(call), "answered" if row["status"] == "answered" else "failed", secs)
    return "ok"


@router.get("/ast/hangup", response_class=PlainTextResponse)
def ast_hangup(request: Request, s: str = "", call: str = "", status: str = "", answered: str = "",
               cause: str = "", peer: str = ""):
    """Hangup handler: final status, talk time and (incoming) which agent answered."""
    ast_guard(request, s)
    if not call.isdigit():
        return "ignored"
    final = DIAL_STATUS.get(status.upper(), "failed" if status else "cancelled")
    try:
        duration = int(float(answered or 0))
    except ValueError:
        duration = 0
    with db.tx() as con:
        row = con.execute("SELECT * FROM calls WHERE id = ?", (int(call),)).fetchone()
        if not row:
            return "ignored"
        agent_id = row["agent_id"]
        m = re.match(r"PJSIP/([^-/]+)-", peer or "")
        if row["direction"] == "in" and m and final == "answered":
            a = con.execute("SELECT id FROM users WHERE sip_ext = ?", (m.group(1),)).fetchone()
            agent_id = a["id"] if a else agent_id
        if row["direction"] == "in" and final in ("no-answer", "busy", "failed", "cancelled"):
            # after-dial decides between 'sent-to-ghl' and 'missed'; a hang-up by the caller is 'missed'
            final = "missed" if final == "cancelled" else row["status"] if row["status"] != "ringing" else "missed"
    rec = os.path.join(config.RECORDINGS_DIR, f"{int(call)}.wav")
    outcomes.finish_call(row["id"], final, duration, cause=cause[:20], agent_id=agent_id,
                         recording=rec if final == "answered" and os.path.exists(rec) else None)
    return "ok"
