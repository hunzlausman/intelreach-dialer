"""Phone carriers for AI / voicemail calls: Telnyx Call Control and Twilio Voice.
(Human agents call through Asterisk + the SIP trunks in Admin → SIP trunks.)"""
import base64
import json
import time
from xml.sax.saxutils import escape, quoteattr

from .. import config, net, vault

TELNYX = "https://api.telnyx.com/v2"
TWILIO = "https://api.twilio.com/2010-04-01/Accounts"


# ------------------------------------------------------------------ Telnyx ----

def telnyx_cfg():
    cfg = vault.load("telnyx")
    if not cfg.get("api_key"):
        raise net.ProviderError("Telnyx API key missing (Admin → Integrations) – to call through your SIP trunk instead, "
                                "set the AI agent's Phone carrier to 'Your SIP trunk'")
    return cfg


def encode_state(d):
    return base64.b64encode(json.dumps(d).encode()).decode()


def decode_state(s):
    try:
        return json.loads(base64.b64decode(s or "").decode() or "{}")
    except ValueError:
        return {}


async def telnyx_post(path, body):
    cfg = telnyx_cfg()
    async with net.client() as c:
        r = await c.post(f"{TELNYX}{path}", json=body, headers={"Authorization": f"Bearer {cfg['api_key']}"})
    return net.check(r, "Telnyx").json()


async def telnyx_post_get(path):
    cfg = telnyx_cfg()
    async with net.client() as c:
        r = await c.get(f"{TELNYX}{path}", headers={"Authorization": f"Bearer {cfg['api_key']}"})
    return net.check(r, "Telnyx").json()


async def telnyx_dial(to, from_number, state, stream_token=None, amd="", record=False, timeout=40):
    cfg = telnyx_cfg()
    if not cfg.get("connection_id"):
        raise net.ProviderError("Telnyx Call Control App ID missing (Admin → Integrations → Telnyx)")
    body = {"connection_id": cfg["connection_id"], "to": to, "from": from_number or cfg.get("from_number", ""),
            "webhook_url": f"{config.PUBLIC_URL}/webhooks/telnyx", "client_state": encode_state(state),
            "timeout_secs": timeout}
    if amd:
        body["answering_machine_detection"] = amd          # e.g. "greeting_end"
    if stream_token:
        body.update(stream_params(stream_token))
    if record:
        body.update({"record": "record-from-answer", "record_format": "mp3", "record_channels": "dual"})
    data = await telnyx_post("/calls", body)
    return data["data"]["call_control_id"]


def stream_params(token):
    """Two-way audio over a WebSocket to our voice engine (8 kHz μ-law RTP payloads)."""
    return {"stream_url": f"{config.WS_PUBLIC_URL}/media/telnyx/{token}", "stream_track": "inbound_track",
            "stream_bidirectional_mode": "rtp", "stream_bidirectional_codec": "PCMU"}


async def telnyx_action(ccid, action, body=None):
    return await telnyx_post(f"/calls/{ccid}/actions/{action}", body or {})


def telnyx_verify(body: bytes, signature_b64: str, timestamp: str, public_key_b64: str, tolerance=300):
    """Telnyx signs '<timestamp>|<raw body>' with Ed25519 (public key: Portal → Keys & Credentials)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not (signature_b64 and timestamp and public_key_b64):
        return False
    try:
        if abs(time.time() - int(timestamp)) > tolerance:
            return False
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), timestamp.encode() + b"|" + body)
        return True
    except (InvalidSignature, ValueError):
        return False


# ------------------------------------------------------------------ Twilio ----

def twilio_auth():
    sid, token = vault.twilio_creds()
    if not (sid and token):
        raise net.ProviderError("Twilio Account SID / Auth Token missing (Admin → Integrations → Twilio)")
    return (sid, token)


def stream_twiml(token, call_id):
    url = f"{config.WS_PUBLIC_URL}/media/twilio/{token}"
    return (f'<Response><Connect><Stream url={quoteattr(url)}>'
            f'<Parameter name="call" value="{int(call_id)}"/></Stream></Connect></Response>')


async def twilio_dial(to, from_number, twiml, call_id, amd=False):
    data = [("To", to), ("From", from_number), ("Twiml", twiml),
            ("StatusCallback", f"{config.PUBLIC_URL}/twilio/ai-status?call={int(call_id)}"),
            ("StatusCallbackMethod", "POST")]
    data += [("StatusCallbackEvent", e) for e in ("answered", "completed")]
    if amd:
        data.append(("MachineDetection", "Enable"))
    auth = twilio_auth()
    async with net.client(auth=auth) as c:
        r = await c.post(f"{TWILIO}/{auth[0]}/Calls.json", data=data)
    return net.check(r, "Twilio").json()["sid"]


async def twilio_update(call_sid, **fields):
    auth = twilio_auth()
    async with net.client(auth=auth) as c:
        r = await c.post(f"{TWILIO}/{auth[0]}/Calls/{call_sid}.json", data=fields)
    return net.check(r, "Twilio").json()


async def twilio_account():
    auth = twilio_auth()
    async with net.client(auth=auth) as c:
        r = await c.get(f"{TWILIO}/{auth[0]}.json")
    return net.check(r, "Twilio").json()


# --------------------------------------------------- carrier-neutral calls ----

async def hangup(provider, external_id):
    if provider == "sip":
        from . import audiosocket
        audiosocket.hangup(external_id)
    elif provider == "telnyx":
        await telnyx_action(external_id, "hangup")
    elif provider == "twilio":
        await twilio_update(external_id, Status="completed")


async def transfer(provider, external_id, target, caller_id="", trunk_id=None):
    """target: +E164 number or sip: URI (e.g. a human agent queue).
    On SIP-trunk calls a sip: target rings the CRM's online agents."""
    if provider == "sip":
        from . import audiosocket
        audiosocket.transfer(external_id, target, caller_id, trunk_id)
    elif provider == "telnyx":
        body = {"to": target}
        if caller_id:
            body["from"] = caller_id
        await telnyx_action(external_id, "transfer", body)
    elif provider == "twilio":
        noun = f"<Sip>{escape(target)}</Sip>" if target.startswith("sip:") else f"<Number>{escape(target)}</Number>"
        await twilio_update(external_id, Twiml=f"<Response><Dial>{noun}</Dial></Response>")
