"""Call events from Telnyx, ElevenLabs and Twilio (AI / voicemail calls)."""
import json
import logging
import os
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from . import config, db, launcher, net, outcomes, phone, vault
from .ai import elevenlabs
from .hooks import twilio_params
from .voice import carriers, engine

log = logging.getLogger("crm.webhooks")
router = APIRouter()

# Telnyx hangup_cause -> CRM status (when nobody answered)
TELNYX_CAUSES = {"timeout": "no-answer", "no_answer": "no-answer", "user_busy": "busy", "call_rejected": "busy",
                 "originator_cancel": "cancelled", "normal_clearing": "no-answer"}


def _call(con, call_id=None, external_id=None):
    if call_id:
        return con.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    if external_id:
        return con.execute("SELECT * FROM calls WHERE external_id = ? ORDER BY id DESC LIMIT 1", (external_id,)).fetchone()
    return None


def _iso(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


# ------------------------------------------------------------------ Telnyx ----

@router.post("/webhooks/telnyx")
async def telnyx_webhook(request: Request):
    body = await request.body()
    pub = vault.key("telnyx", "public_key")
    if not carriers.telnyx_verify(body, request.headers.get("telnyx-signature-ed25519", ""),
                                  request.headers.get("telnyx-timestamp", ""), pub):
        raise HTTPException(403, "Bad Telnyx signature (Admin → Integrations → Telnyx public key)")
    ev = json.loads(body).get("data") or {}
    try:
        await handle_telnyx(ev.get("event_type", ""), ev.get("payload") or {})
    except net.ProviderError as e:
        log.warning("telnyx action failed: %s", e)
    return {"ok": True}


async def handle_telnyx(etype, p):
    ccid = p.get("call_control_id", "")
    state = carriers.decode_state(p.get("client_state"))
    live = engine.LIVE.get(ccid)
    if live:
        live.carrier_event(etype)

    if etype == "call.initiated" and p.get("direction") == "incoming":
        return await telnyx_inbound(p)

    with db.tx() as con:
        call = _call(con, state.get("call_id"), ccid)
    if not call:
        return
    mode = state.get("mode", "")

    if etype == "call.answered":
        with db.tx() as con:
            con.execute("UPDATE calls SET status = 'answered', external_id = ? WHERE id = ? AND status = 'dialing'",
                        (ccid, call["id"]))
        if mode == "assistant" and state.get("assistant_id"):
            await carriers.telnyx_action(ccid, "ai_assistant_start", {"assistant": {"id": state["assistant_id"]}})

    elif etype in ("call.machine.detection.ended", "call.machine.premium.detection.ended") and mode == "voicemail":
        result = (p.get("result") or "").lower()
        machine = "machine" in result
        with db.tx() as con:
            con.execute("UPDATE calls SET amd = ? WHERE id = ?", ("machine" if machine else "human", call["id"]))
            cfg = db.jload(con.execute("SELECT config FROM campaigns WHERE id = ?",
                                       (state.get("campaign_id"),)).fetchone()["config"])
        if not machine:                                    # a person picked up
            await voicemail_human(ccid, call, cfg, state)

    elif etype in ("call.machine.greeting.ended", "call.machine.premium.greeting.ended") and mode == "voicemail":
        with db.tx() as con:
            cfg = db.jload(con.execute("SELECT config FROM campaigns WHERE id = ?",
                                       (state.get("campaign_id"),)).fetchone()["config"])
            con.execute("UPDATE calls SET amd = 'machine' WHERE id = ?", (call["id"],))
        await play_message(ccid, state.get("campaign_id"), cfg)

    elif etype in ("call.playback.ended", "call.speak.ended") and mode == "voicemail":
        with db.tx() as con:
            con.execute("UPDATE calls SET status = 'voicemail-dropped' WHERE id = ?", (call["id"],))
        await carriers.telnyx_action(ccid, "hangup")

    elif etype == "call.recording.saved":
        url = (p.get("recording_urls") or p.get("public_recording_urls") or {}).get("mp3")
        if url:
            outcomes.spawn(save_recording(call["id"], url))

    elif etype == "call.hangup":
        start, end = _iso(p.get("start_time")), _iso(p.get("end_time"))
        with db.tx() as con:
            cur = _call(con, call["id"])
        if cur["status"] in ("answered", "voicemail-dropped"):
            dur = int(end - start) if start and end else int(time.time() - cur["started_at"])
            outcomes.finish_call(call["id"], cur["status"], dur if cur["status"] == "answered" else None,
                                 cause=p.get("hangup_cause", "")[:20])
        else:
            outcomes.finish_call(call["id"], TELNYX_CAUSES.get(p.get("hangup_cause", ""), "failed"),
                                 cause=p.get("hangup_cause", "")[:20])


async def play_message(ccid, campaign_id, cfg):
    path = launcher.voicemail_audio_path(campaign_id)
    if cfg.get("vm_tts") == "elevenlabs" and os.path.exists(path):
        await carriers.telnyx_action(ccid, "playback_start", {"audio_url": launcher.voicemail_audio_url(campaign_id)})
    else:
        await carriers.telnyx_action(ccid, "speak", {"payload": cfg.get("vm_text") or "Hello, please call us back.",
                                                     "voice": cfg.get("vm_voice_id") or "female",
                                                     "language": cfg.get("vm_language") or "en-US"})


async def voicemail_human(ccid, call, cfg, state):
    """Voicemail campaign, but a person answered: what to do is set per campaign."""
    action = cfg.get("on_human", "play")
    if action == "hangup":
        await carriers.telnyx_action(ccid, "hangup")
    elif action == "transfer" and cfg.get("transfer_to"):
        with db.tx() as con:
            con.execute("UPDATE calls SET status = 'answered' WHERE id = ?", (call["id"],))
        await carriers.telnyx_action(ccid, "transfer", {"to": cfg["transfer_to"]})
    elif action == "ai" and cfg.get("ai_agent_id"):
        with db.tx() as con:
            c = db.row(con.execute("SELECT * FROM contacts WHERE id = ?", (call["contact_id"],)).fetchone())
            con.execute("UPDATE calls SET ai_agent_id = ?, status = 'answered' WHERE id = ?", (cfg["ai_agent_id"], call["id"]))
        token = engine.new_token({"call_id": call["id"], "agent_id": int(cfg["ai_agent_id"]),
                                  "vars": engine.contact_vars(c), "external_id": ccid,
                                  "from_number": cfg.get("from_number", "")})
        await carriers.telnyx_action(ccid, "streaming_start", carriers.stream_params(token))
    else:
        await play_message(ccid, state.get("campaign_id"), cfg)


async def telnyx_inbound(p):
    """Someone called one of your Telnyx numbers."""
    ccid = p["call_control_id"]
    with db.tx() as con:
        s = db.get_settings(con)
        agent_id = s.get("telnyx_inbound_agent")
    if not agent_id:
        await carriers.telnyx_action(ccid, "reject", {"cause": "USER_BUSY"})
        return
    with db.tx() as con:
        agent = launcher.load_agent(con, int(agent_id))
        number = phone.normalize(p.get("from", ""), s["default_country"]) or p.get("from", "")
        c = db.row(con.execute("SELECT * FROM contacts WHERE phone = ?", (number,)).fetchone())
        call_id = con.execute(
            "INSERT INTO calls(direction, number, contact_id, ai_agent_id, provider, status, external_id) "
            "VALUES ('in', ?, ?, ?, 'telnyx', 'ringing', ?)", (number, c["id"] if c else None, agent["id"], ccid)).lastrowid
    state = carriers.encode_state({"call_id": call_id, "mode": "assistant" if agent["kind"] == "telnyx" else "custom",
                                   "assistant_id": agent["cfg"].get("tx_assistant_id", "")})
    if agent["kind"] == "custom":
        token = engine.new_token({"call_id": call_id, "agent_id": agent["id"], "vars": engine.contact_vars(c),
                                  "external_id": ccid, "from_number": p.get("to", "")})
        await carriers.telnyx_action(ccid, "answer", {"client_state": state, **carriers.stream_params(token)})
    elif agent["kind"] == "telnyx":
        await carriers.telnyx_action(ccid, "answer", {"client_state": state})      # assistant starts on call.answered
    else:
        await carriers.telnyx_action(ccid, "reject", {"cause": "USER_BUSY"})


async def save_recording(call_id, url):
    os.makedirs(os.path.join(config.MEDIA_DIR, "rec"), exist_ok=True)
    path = os.path.join(config.MEDIA_DIR, "rec", f"{int(call_id)}.mp3")
    async with net.client(timeout=120) as c:
        r = net.check(await c.get(url), "recording download")
    with open(path, "wb") as f:
        f.write(r.content)
    with db.tx() as con:
        con.execute("UPDATE calls SET recording = ? WHERE id = ?", (path, call_id))
    outcomes.maybe_analyze(call_id)


# -------------------------------------------------------------- ElevenLabs ----

@router.post("/webhooks/elevenlabs")
async def elevenlabs_webhook(request: Request):
    body = await request.body()
    if not elevenlabs.verify_webhook(body, request.headers.get("elevenlabs-signature", ""),
                                     vault.key("elevenlabs", "webhook_secret")):
        raise HTTPException(403, "Bad ElevenLabs signature (Admin → Integrations → ElevenLabs webhook secret)")
    msg = json.loads(body)
    data = msg.get("data") or {}
    conv = data.get("conversation_id", "")
    call_sid = ((data.get("metadata") or {}).get("phone_call") or {}).get("call_sid", "")
    with db.tx() as con:
        call = (_call(con, external_id=conv) if conv else None) or (_call(con, external_id=call_sid) if call_sid else None)
    if not call:
        return {"ok": True, "ignored": True}
    if msg.get("type") == "post_call_transcription":
        launcher.finish_elevenlabs(call["id"], data)
    elif msg.get("type") == "call_initiation_failure":
        outcomes.finish_call(call["id"], "failed", cause=str(data.get("failure_reason", ""))[:20])
    return {"ok": True}


# ------------------------------------------------------------------ Twilio ----

@router.post("/twilio/ai-status")
async def twilio_ai_status(request: Request, call: int = 0):
    p = await twilio_params(request)
    st = p.get("CallStatus", "")
    if st == "in-progress":
        with db.tx() as con:
            con.execute("UPDATE calls SET status='answered', external_id=? WHERE id=? AND status='dialing'",
                        (p.get("CallSid", ""), call))
            if p.get("AnsweredBy", "").startswith("machine"):
                con.execute("UPDATE calls SET amd='machine' WHERE id=?", (call,))
    elif st in ("completed", "no-answer", "busy", "failed", "canceled"):
        status = {"completed": "answered", "canceled": "cancelled"}.get(st, st)
        with db.tx() as con:
            cur = _call(con, call)
        if cur and status == "answered" and cur["status"] == "dialing":
            status = "no-answer"
        outcomes.finish_call(call, status, int(p.get("CallDuration") or 0) if status == "answered" else None)
    return PlainTextResponse("ok")
