"""Provider API keys (Twilio, OpenAI, Anthropic, ElevenLabs, Telnyx …), stored encrypted
in the integrations table with the CRM_SECRET_KEY Fernet key."""
import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken

from . import config, db

# provider -> fields (secret fields are never sent back to the browser); "required" = what makes it configured
PROVIDERS = {
    "twilio":     {"label": "Twilio", "fields": {"account_sid": False, "auth_token": True},
                   "required": ("account_sid", "auth_token")},
    "anthropic":  {"label": "Anthropic Claude", "fields": {"api_key": True}},
    "openai":     {"label": "OpenAI", "fields": {"api_key": True, "base_url": False}},
    "gemini":     {"label": "Google Gemini", "fields": {"api_key": True}},
    "custom_llm": {"label": "OpenAI-compatible (Groq, DeepSeek, OpenRouter, Ollama…)",
                   "fields": {"base_url": False, "api_key": True}},
    "assemblyai": {"label": "AssemblyAI", "fields": {"api_key": True}},
    "deepgram":   {"label": "Deepgram", "fields": {"api_key": True, "model": False, "language": False}},
    "elevenlabs": {"label": "ElevenLabs", "fields": {"api_key": True, "webhook_secret": True}},
    "telnyx":     {"label": "Telnyx", "fields": {"api_key": True, "connection_id": False, "public_key": False,
                                                 "from_number": False}},
}


def _fernet():
    key = config.SECRET_KEY
    if not key:
        # dev / tests: derive a key from the Asterisk secret so it is at least not plain text
        key = base64.urlsafe_b64encode(hashlib.sha256((config.AST_SECRET or "dev").encode()).digest()).decode()
    return Fernet(key.encode())


def load(provider):
    with db.tx() as con:
        r = con.execute("SELECT secret FROM integrations WHERE provider = ?", (provider,)).fetchone()
    if not r:
        return {}
    try:
        return json.loads(_fernet().decrypt(r["secret"].encode()))
    except (InvalidToken, ValueError):
        return {}


def save(provider, values):
    """Merge: an empty secret field keeps the stored value (the UI never sees secrets)."""
    spec = PROVIDERS[provider]["fields"]
    cur = load(provider)
    for k, secret in spec.items():
        if k not in values:
            continue
        v = str(values[k] or "").strip()
        if secret and not v:
            continue
        cur[k] = v
    token = _fernet().encrypt(json.dumps(cur).encode()).decode()
    with db.tx() as con:
        con.execute("INSERT INTO integrations(provider, secret) VALUES (?, ?) ON CONFLICT(provider) "
                    "DO UPDATE SET secret=excluded.secret, updated_at=strftime('%s','now')", (provider, token))
    return cur


def public(provider):
    """What the admin UI may see: non-secret values + which secrets are set."""
    cur = load(provider)
    out = {}
    for k, secret in PROVIDERS[provider]["fields"].items():
        out[k] = ("•••• " + cur[k][-4:] if cur.get(k) else "") if secret else cur.get(k, "")
    out["configured"] = all(cur.get(k) for k in PROVIDERS[provider].get("required", ("api_key",)))
    return out


def key(provider, field="api_key"):
    return load(provider).get(field, "")


def twilio_creds():
    """(Account SID, Auth Token) from Admin → Integrations → Twilio, else the old .env values."""
    cur = load("twilio")
    if cur.get("account_sid") and cur.get("auth_token"):
        return cur["account_sid"], cur["auth_token"]
    return config.TWILIO_SID, config.TWILIO_TOKEN


def encrypt(text):
    return _fernet().encrypt((text or "").encode()).decode() if text else ""


def decrypt(token):
    try:
        return _fernet().decrypt(token.encode()).decode() if token else ""
    except (InvalidToken, ValueError):
        return ""
