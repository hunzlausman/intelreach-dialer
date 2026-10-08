"""Admin API: agents, settings, SIP trunks and numbers, connecting the Twilio number."""
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import config, db, hooks, phone, security, trunks, twilio_api, vault
from .ai import stt
from .security import require_admin

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

SETTING_KEYS = ("company_name", "inbound_mode", "ring_timeout", "default_country",
                "allowed_prefixes", "blocked_prefixes", "max_call_minutes", "record_calls", "transcribe_recordings",
                "live_captions", "analyze_calls", "analysis_llm", "analysis_model", "inbound_ai_agent",
                "telnyx_inbound_agent", "default_trunk", "stt_provider")
FLAGS = ("record_calls", "transcribe_recordings", "live_captions", "analyze_calls")


# ---------------------------------------------------------------- agents ----

def free_ext(con):
    r = con.execute("SELECT ext FROM sip_pool WHERE ext NOT IN (SELECT sip_ext FROM users WHERE sip_ext IS NOT NULL) "
                    "ORDER BY CAST(ext AS INTEGER) LIMIT 1").fetchone()
    return r["ext"] if r else None


@router.get("/users")
def list_users():
    now = int(time.time())
    with db.tx() as con:
        users = db.rows(con.execute("SELECT id, email, name, role, sip_ext, active, available, last_seen FROM users ORDER BY name"))
        free = con.execute("SELECT COUNT(*) FROM sip_pool WHERE ext NOT IN (SELECT sip_ext FROM users WHERE sip_ext IS NOT NULL)").fetchone()[0]
    for u in users:
        u["online"] = u["last_seen"] > now - config.ONLINE_SECONDS
    return {"items": users, "freeLines": free}


class UserIn(BaseModel):
    email: str
    name: str
    role: str = "agent"
    password: str = ""
    active: bool = True


@router.post("/users")
def create_user(body: UserIn):
    if len(body.password) < 8:
        raise HTTPException(400, "Password must have at least 8 characters")
    if body.role not in ("admin", "agent"):
        raise HTTPException(400, "Role must be admin or agent")
    with db.tx() as con:
        if con.execute("SELECT 1 FROM users WHERE email = ?", (body.email.strip(),)).fetchone():
            raise HTTPException(409, "A user with this email already exists")
        ext = free_ext(con)
        uid = con.execute("INSERT INTO users(email, name, role, pw_hash, sip_ext, active) VALUES (?,?,?,?,?,?)",
                          (body.email.strip(), body.name.strip(), body.role, security.hash_password(body.password),
                           ext, int(body.active))).lastrowid
    return {"id": uid, "sip_ext": ext, "warning": "" if ext else "No free phone line – run gen_agent_pool.py with a bigger --count"}


@router.put("/users/{uid}")
def update_user(uid: int, body: UserIn, admin=Depends(require_admin)):
    if body.role not in ("admin", "agent"):
        raise HTTPException(400, "Role must be admin or agent")
    if uid == admin["id"] and (body.role != "admin" or not body.active):
        raise HTTPException(400, "You cannot remove your own admin access")
    with db.tx() as con:
        u = con.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if not u:
            raise HTTPException(404, "User not found")
        con.execute("UPDATE users SET email=?, name=?, role=?, active=? WHERE id=?",
                    (body.email.strip(), body.name.strip(), body.role, int(body.active), uid))
        if body.password:
            if len(body.password) < 8:
                raise HTTPException(400, "Password must have at least 8 characters")
            con.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (security.hash_password(body.password), uid))
            con.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        if not body.active:
            con.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        elif not u["sip_ext"]:
            con.execute("UPDATE users SET sip_ext = ? WHERE id = ?", (free_ext(con), uid))
    return {"ok": True}


# -------------------------------------------------------------- settings ----

@router.get("/settings")
def get_settings():
    with db.tx() as con:
        return {**db.get_settings(con),
                "_trunks": [{"id": t["id"], "name": t["name"], "vendor": t["vendor"]} for t in
                            con.execute("SELECT id, name, vendor FROM sip_trunks WHERE enabled = 1 ORDER BY id")],
                "_numbers": db.rows(con.execute("SELECT number, label, trunk_id FROM phone_numbers ORDER BY number"))}


@router.put("/settings")
def put_settings(body: dict):
    with db.tx() as con:
        for k in SETTING_KEYS:
            if k not in body:
                continue
            v = str(body[k]).strip()
            if k == "inbound_mode" and v not in hooks.INBOUND_MODES:
                raise HTTPException(400, "Unknown inbound mode")
            if k in FLAGS:
                v = "1" if v in ("1", "true", "True", "on") else "0"
            if k == "stt_provider" and v not in stt.PROVIDERS:
                raise HTTPException(400, "Speech-to-text must be assemblyai or deepgram")
            if k == "default_trunk" and v and not trunks.find(con, v):
                raise HTTPException(400, "Choose one of your enabled SIP trunks")
            if k in ("inbound_ai_agent", "telnyx_inbound_agent") and v and not v.isdigit():
                raise HTTPException(400, "Choose an AI agent")
            if k in ("ring_timeout", "max_call_minutes") and not v.isdigit():
                raise HTTPException(400, f"{k} must be a whole number")
            if k == "default_country" and v and not phone.E164.match(v + "0000000"):
                raise HTTPException(400, "Default country must look like +1 or +44")
            db.set_setting(con, k, v)
        return db.get_settings(con)


# ---------------------------------------------------- SIP trunks / numbers ----

@router.get("/trunks")
def list_trunks():
    with db.tx() as con:
        items = [trunks.public(t) for t in con.execute("SELECT * FROM sip_trunks ORDER BY id")]
        numbers = db.rows(con.execute("SELECT * FROM phone_numbers ORDER BY number"))
        s = db.get_settings(con)
    return {"items": items, "numbers": numbers, "vendors": trunks.VENDORS, "defaultTrunk": s.get("default_trunk", ""),
            "twilioNumber": s.get("twilio_number", ""),
            "inboundUri": f"sip:<number>@{config.SIP_INBOUND_URI.split('@')[-1].split(';')[0]}"}


def _save_trunk(tid, body):
    with db.tx() as con:
        cur = None
        if tid:
            cur = con.execute("SELECT * FROM sip_trunks WHERE id = ?", (tid,)).fetchone()
            if not cur:
                raise HTTPException(404, "Trunk not found")
        v = trunks.clean(body, cur)
        if cur:
            con.execute(f"UPDATE sip_trunks SET {', '.join(k + ' = ?' for k in v)} WHERE id = ?", (*v.values(), tid))
        else:
            tid = con.execute(f"INSERT INTO sip_trunks({', '.join(v)}) VALUES ({', '.join('?' * len(v))})",
                              tuple(v.values())).lastrowid
        warning = trunks.apply(con)
    return {"id": tid, "endpoint": trunks.endpoint(tid), "warning": warning}


@router.post("/trunks")
def create_trunk(body: dict):
    return _save_trunk(None, body)


@router.put("/trunks/{tid}")
def update_trunk(tid: int, body: dict):
    return _save_trunk(tid, body)


@router.delete("/trunks/{tid}")
def delete_trunk(tid: int):
    with db.tx() as con:
        con.execute("DELETE FROM sip_trunks WHERE id = ?", (tid,))
        if db.get_settings(con).get("default_trunk") == str(tid):
            db.set_setting(con, "default_trunk", "")
        warning = trunks.apply(con)
    return {"ok": True, "warning": warning}


def _save_number(nid, body):
    with db.tx() as con:
        v = trunks.clean_number(con, body)
        dup = con.execute("SELECT id FROM phone_numbers WHERE number = ?", (v["number"],)).fetchone()
        if dup and dup["id"] != nid:
            raise HTTPException(409, f"{v['number']} is already in the list")
        if nid:
            if not con.execute("UPDATE phone_numbers SET number=?, label=?, trunk_id=?, inbound=? WHERE id=?",
                               (*v.values(), nid)).rowcount:
                raise HTTPException(404, "Number not found")
        else:
            nid = con.execute("INSERT INTO phone_numbers(number, label, trunk_id, inbound) VALUES (?, ?, ?, ?)",
                              tuple(v.values())).lastrowid
    return {"id": nid}


@router.post("/numbers")
def create_number(body: dict):
    return _save_number(None, body)


@router.put("/numbers/{nid}")
def update_number(nid: int, body: dict):
    return _save_number(nid, body)


@router.delete("/numbers/{nid}")
def delete_number(nid: int):
    with db.tx() as con:
        con.execute("DELETE FROM phone_numbers WHERE id = ?", (nid,))
    return {"ok": True}


# ---------------------------------------------------------------- Twilio ----

def our_url():
    return f"{config.PUBLIC_URL}/twilio/voice"


@router.get("/twilio")
def twilio_status():
    with db.tx() as con:
        s = db.get_settings(con)
    out = {"number": s["twilio_number"], "ghlUrl": s["ghl_voice_url"], "ghlApp": s["ghl_voice_app_sid"],
           "ourUrl": our_url(), "credentials": all(vault.twilio_creds()),
           "connected": False, "currentUrl": ""}
    if s["twilio_number"] and out["credentials"]:
        try:
            n = twilio_api.find_number(s["twilio_number"])
            out["currentUrl"] = n.get("voice_url") or ""
            out["currentApp"] = n.get("voice_application_sid") or ""
            out["trunk"] = n.get("trunk_sid") or ""
            out["connected"] = out["currentUrl"] == our_url() and not out["currentApp"]
        except HTTPException as e:
            out["error"] = e.detail
    return out


class ConnectIn(BaseModel):
    number: str


@router.post("/twilio/connect")
def twilio_connect(body: ConnectIn):
    """Use the number as caller ID and send its incoming calls to the CRM first.
    GHL's webhook is saved and used as the fallback (and restored on disconnect)."""
    num = phone.normalize(body.number, "")
    if not num:
        raise HTTPException(400, "Enter the number in +<country code><number> form")
    n = twilio_api.find_number(num)
    if n.get("trunk_sid"):
        raise HTTPException(409, "This number is attached to an Elastic SIP Trunk in Twilio. Remove it from the trunk "
                                 "(Trunk → Numbers) – the trunk is only used for outgoing calls.")
    with db.tx() as con:
        s = db.get_settings(con)
        app_sid = n.get("voice_application_sid") or ""
        url, method = n.get("voice_url") or "", n.get("voice_method") or "POST"
        if app_sid:                       # GHL used a TwiML App: the fallback is that app's URL
            a = twilio_api.get_application(app_sid)
            url, method = a.get("voice_url") or "", a.get("voice_method") or "POST"
        if url and url != our_url():      # never save ourselves as the fallback
            db.set_setting(con, "ghl_voice_url", url)
            db.set_setting(con, "ghl_voice_method", method)
            db.set_setting(con, "ghl_voice_app_sid", app_sid)
        db.set_setting(con, "twilio_number", num)
        db.set_setting(con, "twilio_number_sid", n["sid"])
        s = db.get_settings(con)
    if s["inbound_mode"] != "ghl_only":
        twilio_api.update_number(n["sid"], VoiceUrl=our_url(), VoiceMethod="POST", VoiceApplicationSid="",
                                 VoiceFallbackUrl=s["ghl_voice_url"] or "", VoiceFallbackMethod=s["ghl_voice_method"])
    return {"ok": True, "number": num, "ghlUrl": s["ghl_voice_url"]}


@router.post("/twilio/disconnect")
def twilio_disconnect():
    """Give incoming calls back to GHL exactly as they were. Outgoing CRM calls keep working."""
    with db.tx() as con:
        s = db.get_settings(con)
    if not s["twilio_number_sid"]:
        raise HTTPException(400, "No number connected")
    if s["ghl_voice_app_sid"]:
        twilio_api.update_number(s["twilio_number_sid"], VoiceApplicationSid=s["ghl_voice_app_sid"])
    elif s["ghl_voice_url"]:
        twilio_api.update_number(s["twilio_number_sid"], VoiceUrl=s["ghl_voice_url"],
                                 VoiceMethod=s["ghl_voice_method"], VoiceFallbackUrl="")
    else:
        raise HTTPException(400, "GHL's original webhook is unknown – set it again in GHL (re-save the number there)")
    return {"ok": True}
