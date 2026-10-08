"""ElevenLabs: streaming voices for the custom pipeline, voicemail audio files,
and ElevenLabs Conversational AI agents ("direct" agents)."""
import hashlib
import hmac
import time

from .. import net, vault

API = "https://api.elevenlabs.io/v1"
DEFAULT_TTS_MODEL = "eleven_flash_v2_5"     # lowest latency


def _headers():
    k = vault.key("elevenlabs")
    if not k:
        raise net.ProviderError("ElevenLabs API key missing (Admin → Integrations)")
    return {"xi-api-key": k}


async def tts_stream(text, voice_id, model_id="", fmt="ulaw_8000"):
    """Yields raw audio bytes (8 kHz μ-law by default – what phone networks use)."""
    async with net.client(timeout=60) as c:
        async with c.stream("POST", f"{API}/text-to-speech/{voice_id}/stream", params={"output_format": fmt},
                            headers=_headers(), json={"text": text, "model_id": model_id or DEFAULT_TTS_MODEL}) as r:
            if r.status_code >= 400:
                await r.aread()
                net.check(r, "ElevenLabs")
            async for chunk in r.aiter_bytes():
                if chunk:
                    yield chunk


async def tts_file(text, voice_id, path, model_id="eleven_multilingual_v2"):
    with open(path, "wb") as f:
        async for chunk in tts_stream(text, voice_id, model_id, fmt="mp3_44100_128"):
            f.write(chunk)
    return path


async def voices():
    async with net.client() as c:
        r = net.check(await c.get(f"{API}/voices", headers=_headers()), "ElevenLabs")
    return [{"id": v["voice_id"], "name": v["name"]} for v in r.json().get("voices", [])]


async def agents():
    async with net.client() as c:
        r = net.check(await c.get(f"{API}/convai/agents", headers=_headers()), "ElevenLabs")
    return [{"id": a["agent_id"], "name": a.get("name", "")} for a in r.json().get("agents", [])]


async def phone_numbers():
    async with net.client() as c:
        r = net.check(await c.get(f"{API}/convai/phone-numbers", headers=_headers()), "ElevenLabs")
    data = r.json()
    items = data if isinstance(data, list) else data.get("phone_numbers", [])
    return [{"id": p.get("phone_number_id"), "number": p.get("phone_number"), "provider": p.get("provider", "")}
            for p in items]


async def outbound_call(agent_id, phone_number_id, to_number, variables, via="sip_trunk"):
    """Start a call with an ElevenLabs agent. via = sip_trunk (e.g. a Telnyx number
    imported into ElevenLabs) or twilio (a Twilio number imported into ElevenLabs)."""
    path = "twilio/outbound-call" if via == "twilio" else "sip-trunk/outbound-call"
    body = {"agent_id": agent_id, "agent_phone_number_id": phone_number_id, "to_number": to_number,
            "conversation_initiation_client_data": {"dynamic_variables": variables}}
    async with net.client() as c:
        r = net.check(await c.post(f"{API}/convai/{path}", headers=_headers(), json=body), "ElevenLabs").json()
    if r.get("success") is False:
        raise net.ProviderError(f"ElevenLabs: {r.get('message')}")
    return r.get("conversation_id") or ""


async def register_twilio_call(agent_id, from_number, to_number, variables):
    """Inbound call already on Twilio -> TwiML that connects it to the ElevenLabs agent."""
    body = {"agent_id": agent_id, "from_number": from_number, "to_number": to_number, "direction": "inbound",
            "conversation_initiation_client_data": {"dynamic_variables": variables}}
    async with net.client() as c:
        r = net.check(await c.post(f"{API}/convai/twilio/register-call", headers=_headers(), json=body), "ElevenLabs")
    return r.text


async def conversation(conversation_id):
    async with net.client() as c:
        return net.check(await c.get(f"{API}/convai/conversations/{conversation_id}", headers=_headers()),
                         "ElevenLabs").json()


def parse_conversation(data):
    """ElevenLabs conversation / post-call webhook data -> CRM fields."""
    transcript = [{"role": "agent" if t.get("role") == "agent" else "contact", "text": t.get("message") or ""}
                  for t in data.get("transcript") or [] if t.get("message")]
    analysis = data.get("analysis") or {}
    fields = {k: (v or {}).get("value") for k, v in (analysis.get("data_collection_results") or {}).items()}
    meta = data.get("metadata") or {}
    return {
        "transcript": transcript,
        "summary": analysis.get("transcript_summary") or "",
        "success": analysis.get("call_successful"),         # success | failure | unknown
        "fields": fields,
        "duration": int(meta.get("call_duration_secs") or 0),
        "status": data.get("status", ""),
    }


def verify_webhook(body: bytes, header: str, secret: str, tolerance=1800):
    """ElevenLabs-Signature: t=<unix>,v0=<hex hmac_sha256(secret, f'{t}.{body}')>"""
    if not secret or not header:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    t, sig = parts.get("t", ""), parts.get("v0", "")
    if not t.isdigit() or abs(time.time() - int(t)) > tolerance:
        return False
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, sig)
