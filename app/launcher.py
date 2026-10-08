"""Places AI and voicemail calls.

AI agent kinds
  custom      our own pipeline (voice/engine.py) over Telnyx or Twilio media streams
  elevenlabs  an ElevenLabs Conversational AI agent (ElevenLabs runs the conversation)
  telnyx      a Telnyx AI Assistant (Telnyx runs the conversation, started on answer)
"""
import asyncio
import logging
import os

from . import config, db, net, outcomes
from .ai import elevenlabs
from .voice import carriers, engine

log = logging.getLogger("crm.launcher")


def load_agent(con, agent_id):
    a = con.execute("SELECT * FROM ai_agents WHERE id = ?", (agent_id,)).fetchone()
    if not a:
        raise net.ProviderError("AI agent not found")
    return {"id": a["id"], "name": a["name"], "kind": a["kind"], "cfg": db.jload(a["config"])}


def _new_call(con, number, contact_id, provider, agent_id=None, campaign_id=None, lead_id=None, user_id=None):
    return con.execute(
        """INSERT INTO calls(direction, number, contact_id, agent_id, ai_agent_id, campaign_id, lead_id, provider, status)
           VALUES ('out', ?, ?, ?, ?, ?, ?, ?, 'dialing')""",
        (number, contact_id, user_id, agent_id, campaign_id, lead_id, provider)).lastrowid


def _caller_id(cfg, settings, carrier):
    if cfg.get("from_number"):
        return cfg["from_number"]
    if carrier == "twilio":
        return settings.get("twilio_number", "")
    return ""                                  # telnyx: integration default from_number


async def place_ai_call(agent_id, number, contact=None, campaign_id=None, lead_id=None, user_id=None, extra_vars=None):
    with db.tx() as con:
        agent = load_agent(con, agent_id)
        s = db.get_settings(con)
        cfg = agent["cfg"]
        provider = {"custom": cfg.get("carrier", "telnyx"), "elevenlabs": "elevenlabs", "telnyx": "telnyx"}[agent["kind"]]
        call_id = _new_call(con, number, (contact or {}).get("id"), provider, agent_id, campaign_id, lead_id, user_id)
        if lead_id:
            con.execute("UPDATE campaign_leads SET last_call_id = ?, attempts = attempts + 1 WHERE id = ?", (call_id, lead_id))
    variables = {**engine.contact_vars(contact), **(extra_vars or {})}
    try:
        if agent["kind"] == "custom":
            from_number = _caller_id(cfg, s, provider)
            token = engine.new_token({"call_id": call_id, "agent_id": agent_id, "vars": variables,
                                      "from_number": from_number})
            if provider == "twilio":
                ext = await carriers.twilio_dial(number, from_number, carriers.stream_twiml(token, call_id), call_id)
            else:
                ext = await carriers.telnyx_dial(number, from_number, {"call_id": call_id, "mode": "custom"},
                                                 stream_token=token, record=bool(cfg.get("record")))
            engine.PENDING[token]["external_id"] = ext
        elif agent["kind"] == "elevenlabs":
            ext = await elevenlabs.outbound_call(cfg["el_agent_id"], cfg["el_phone_number_id"], number, variables,
                                                 via=cfg.get("el_phone_type", "sip_trunk"))
            outcomes.spawn(poll_elevenlabs(call_id, ext))
        else:
            ext = await carriers.telnyx_dial(number, _caller_id(cfg, s, "telnyx"),
                                             {"call_id": call_id, "mode": "assistant",
                                              "assistant_id": cfg.get("tx_assistant_id", ""), "vars": variables},
                                             record=bool(cfg.get("record")))
    except Exception as e:
        outcomes.finish_call(call_id, "failed", cause=str(e)[:60])
        raise
    with db.tx() as con:
        con.execute("UPDATE calls SET external_id = ? WHERE id = ?", (ext, call_id))
    return call_id


async def place_voicemail(campaign_id, cfg, contact, lead_id):
    """Telnyx call with answering-machine detection; the message plays after the beep."""
    with db.tx() as con:
        call_id = _new_call(con, contact["phone"], contact["id"], "telnyx", campaign_id=campaign_id, lead_id=lead_id)
        con.execute("UPDATE campaign_leads SET last_call_id = ?, attempts = attempts + 1 WHERE id = ?", (call_id, lead_id))
    try:
        ext = await carriers.telnyx_dial(contact["phone"], cfg.get("from_number", ""),
                                         {"call_id": call_id, "mode": "voicemail", "campaign_id": campaign_id},
                                         amd="greeting_end")
    except Exception as e:
        outcomes.finish_call(call_id, "failed", cause=str(e)[:60])
        raise
    with db.tx() as con:
        con.execute("UPDATE calls SET external_id = ? WHERE id = ?", (ext, call_id))
    return call_id


def voicemail_audio_path(campaign_id):
    return os.path.join(config.MEDIA_DIR, "vm", f"{int(campaign_id)}.mp3")


def voicemail_audio_url(campaign_id):
    return f"{config.PUBLIC_URL}/public/vm/{int(campaign_id)}.mp3"


async def build_voicemail_audio(campaign_id, cfg):
    """ElevenLabs voice -> mp3 that Telnyx plays (only when the text/voice changed)."""
    if cfg.get("vm_tts") != "elevenlabs" or not cfg.get("vm_text"):
        return None
    os.makedirs(os.path.dirname(voicemail_audio_path(campaign_id)), exist_ok=True)
    await elevenlabs.tts_file(cfg["vm_text"], cfg.get("vm_voice_id", ""), voicemail_audio_path(campaign_id))
    return voicemail_audio_url(campaign_id)


async def poll_elevenlabs(call_id, conversation_id, every=20, limit=3 * 3600):
    """Backup for the ElevenLabs post-call webhook: fetch the result when the call is over."""
    waited = 0
    while waited < limit:
        await asyncio.sleep(every)
        waited += every
        with db.tx() as con:
            r = con.execute("SELECT ended_at FROM calls WHERE id = ?", (call_id,)).fetchone()
        if not r or r["ended_at"]:
            return
        try:
            data = await elevenlabs.conversation(conversation_id)
        except Exception as e:
            log.info("elevenlabs poll: %s", e)
            continue
        if data.get("status") in ("done", "failed"):
            finish_elevenlabs(call_id, data)
            return


def finish_elevenlabs(call_id, data):
    import json
    p = elevenlabs.parse_conversation(data)
    spoke = any(t["role"] == "contact" for t in p["transcript"])
    status = "answered" if spoke else ("failed" if p["status"] == "failed" else "no-answer")
    outcomes.finish_call(call_id, status, p["duration"], transcript=json.dumps(p["transcript"], ensure_ascii=False)
                         if p["transcript"] else None, summary=p["summary"] or None,
                         ai_fields=json.dumps(p["fields"], ensure_ascii=False) if p["fields"] else None)
    if p["fields"]:
        outcomes.merge_contact_fields(call_id, p["fields"])
