"""Places AI and voicemail calls.

AI agent kinds
  custom      our own pipeline (voice/engine.py) over Telnyx or Twilio media streams,
              or over your own SIP trunks (carrier "sip": Asterisk + AudioSocket)
  elevenlabs  an ElevenLabs Conversational AI agent (ElevenLabs runs the conversation)
  telnyx      a Telnyx AI Assistant (Telnyx runs the conversation, started on answer)
"""
import asyncio
import logging
import os
import uuid

from . import config, db, net, outcomes, trunks, vault
from .ai import elevenlabs
from .voice import audiosocket, carriers, engine

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


def fix_agent_carriers():
    """Custom agents saved with a carrier whose API is not set up (e.g. 'telnyx' – the old default) can never
    place a call; switch them to the SIP trunks. Runs at startup."""
    import json
    unusable = {"telnyx": not vault.key("telnyx"), "twilio": not all(vault.twilio_creds())}
    with db.tx() as con:
        if not con.execute("SELECT 1 FROM sip_trunks WHERE enabled = 1").fetchone():
            return
        for a in con.execute("SELECT id, name, config FROM ai_agents WHERE kind = 'custom'").fetchall():
            cfg = db.jload(a["config"])
            if unusable.get(cfg.get("carrier")):
                cfg["carrier"] = "sip"
                if cfg.get("tts_provider") == "telnyx":
                    cfg["tts_provider"] = "elevenlabs"
                con.execute("UPDATE ai_agents SET config = ? WHERE id = ?", (json.dumps(cfg), a["id"]))
                log.info("AI agent %s now calls through the SIP trunk (its carrier's API is not set up)", a["name"])


def ai_provider(agent, carrier=""):
    """How this agent's call is placed. carrier: a campaign's choice – 'sip', 'telnyx', 'twilio',
    or 'agent' / '' = the agent's own Phone carrier. Only Custom agents can be re-routed."""
    if agent["kind"] == "custom":
        return (carrier if carrier not in ("", "agent") else "") or agent["cfg"].get("carrier") or "sip"
    return agent["kind"]                       # elevenlabs / telnyx: the provider runs the call


def route_problem(con, agent, carrier=""):
    """'' if the call can be placed, else what is missing (shown when a campaign starts / pauses)."""
    p = ai_provider(agent, carrier)
    if agent["kind"] == "telnyx" and not vault.key("telnyx"):
        return (f"'{agent['name']}' is a Telnyx AI Assistant – it runs inside Telnyx and needs the Telnyx API key "
                "(missing in Admin → Integrations). To call through your SIP trunk, create an AI agent of type "
                "'Custom' (Deepgram + LLM + ElevenLabs) and use that one.")
    if agent["kind"] == "elevenlabs" and not vault.key("elevenlabs"):
        return (f"'{agent['name']}' is an ElevenLabs agent and the ElevenLabs API key is missing (Admin → Integrations). "
                "To call through your SIP trunk, use an AI agent of type 'Custom'.")
    if p == "sip":
        if not con.execute("SELECT 1 FROM sip_trunks WHERE enabled = 1").fetchone():
            return "No SIP trunk configured – missing in Admin → SIP trunks"
        if agent["cfg"].get("tts_provider") == "telnyx":
            return f"'{agent['name']}' uses a Telnyx voice, which only works on Telnyx API calls – choose ElevenLabs (missing voice)"
        if not vault.key("elevenlabs"):
            return "ElevenLabs API key missing (Admin → Integrations) – the AI agent's voice"
        from .ai import stt
        if stt.missing(agent["cfg"].get("stt_provider", "")):
            return stt.missing(agent["cfg"].get("stt_provider", ""))
        if not audiosocket.PORT:
            return "AI over SIP trunks is not running in the CRM (AudioSocket server missing) – see journalctl -u intelreach-crm"
    elif p == "telnyx" and not vault.key("telnyx"):
        return ("Telnyx API key missing (Admin → Integrations) – or call through your SIP trunk: set the campaign's "
                "'Calls go out through' (or the AI agent's Phone carrier) to 'Your SIP trunk'")
    elif p == "twilio" and not all(vault.twilio_creds()):
        return "Twilio Account SID / Auth Token missing (Admin → Integrations) – or use 'Your SIP trunk'"
    elif p == "elevenlabs" and not vault.key("elevenlabs"):
        return "ElevenLabs API key missing (Admin → Integrations)"
    return ""


async def place_ai_call(agent_id, number, contact=None, campaign_id=None, lead_id=None, user_id=None, extra_vars=None,
                        carrier="", caller_id=""):
    """carrier / caller_id: a campaign's choices, over the agent's own."""
    with db.tx() as con:
        agent = load_agent(con, agent_id)
        problem = route_problem(con, agent, carrier)
        if problem:
            raise net.ProviderError(problem)
        s = db.get_settings(con)
        cfg = {**agent["cfg"], **({"from_number": caller_id} if caller_id else {})}
        provider = ai_provider(agent, carrier)
        call_id = _new_call(con, number, (contact or {}).get("id"), provider, agent_id, campaign_id, lead_id, user_id)
        if lead_id:
            con.execute("UPDATE campaign_leads SET last_call_id = ?, attempts = attempts + 1 WHERE id = ?", (call_id, lead_id))
    variables = {**engine.contact_vars(contact), **(extra_vars or {})}
    try:
        if agent["kind"] == "custom" and provider == "sip":
            ext = place_sip_ai_call(call_id, agent_id, number, variables, cfg, s)
        elif agent["kind"] == "custom":
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


def place_sip_ai_call(call_id, agent_id, number, variables, cfg, settings):
    """Through Asterisk and a trunk from Admin → SIP trunks (the agent's caller ID picks the trunk)."""
    if not audiosocket.PORT:
        raise net.ProviderError("AI over SIP trunks is not running (AudioSocket server) – see the CRM log")
    with db.tx() as con:
        t, caller = trunks.route(con, settings, {"caller_id": cfg.get("from_number", "")})
    if not t:
        raise net.ProviderError("No SIP trunk configured (Admin → SIP trunks)")
    if not caller:
        raise net.ProviderError("No caller ID for the SIP trunk (Admin → SIP trunks → Numbers)")
    uid = str(uuid.uuid4())
    engine.new_token({"call_id": call_id, "agent_id": agent_id, "vars": variables, "from_number": caller,
                      "trunk_id": t["id"], "external_id": uid}, token=uid)
    try:
        audiosocket.originate(trunks.endpoint(t["id"]), trunks.dial_number(t, number), caller, call_id, uid)
    except RuntimeError as e:
        engine.PENDING.pop(uid, None)
        raise net.ProviderError(str(e))
    return uid


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
