"""SQLite storage. One short-lived connection per request (WAL mode)."""
import json
import os
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'agent',      -- admin | agent
    pw_hash     TEXT NOT NULL,
    sip_ext     TEXT UNIQUE,
    active      INTEGER NOT NULL DEFAULT 1,
    available   INTEGER NOT NULL DEFAULT 1,         -- takes incoming calls
    last_seen   INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sip_pool (
    ext         TEXT PRIMARY KEY,
    password    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL,                      -- E.164 (+447700900123)
    email       TEXT NOT NULL DEFAULT '',
    company     TEXT NOT NULL DEFAULT '',
    country     TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'new',        -- new | contacted | interested | customer | closed
    notes       TEXT NOT NULL DEFAULT '',
    owner_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS contacts_phone ON contacts(phone);
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY,
    direction   TEXT NOT NULL,                      -- out | in
    number      TEXT NOT NULL,                      -- the other party, E.164
    contact_id  INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    agent_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    status      TEXT NOT NULL DEFAULT 'new',
    duration    INTEGER NOT NULL DEFAULT 0,         -- talk time, seconds
    cause       TEXT NOT NULL DEFAULT '',
    twilio_sid  TEXT NOT NULL DEFAULT '',
    disposition TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    started_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    ended_at    INTEGER
);
CREATE INDEX IF NOT EXISTS calls_started ON calls(started_at);
CREATE INDEX IF NOT EXISTS calls_contact ON calls(contact_id);
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS integrations (
    provider    TEXT PRIMARY KEY,                   -- openai | anthropic | gemini | custom_llm | assemblyai | elevenlabs | telnyx
    secret      TEXT NOT NULL,                      -- encrypted JSON
    updated_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS ai_agents (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,                      -- custom | elevenlabs | telnyx
    config      TEXT NOT NULL DEFAULT '{}',
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS campaigns (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,                      -- power | ai | voicemail
    status      TEXT NOT NULL DEFAULT 'draft',      -- draft | running | paused | completed
    config      TEXT NOT NULL DEFAULT '{}',
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS campaign_leads (
    id          INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'pending',    -- pending | calling | retry | done | failed | dnc
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_at     INTEGER NOT NULL DEFAULT 0,
    agent_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    last_call_id INTEGER,
    result      TEXT NOT NULL DEFAULT '',
    tz          TEXT NOT NULL DEFAULT '',
    updated_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(campaign_id, contact_id)
);
CREATE INDEX IF NOT EXISTS leads_due ON campaign_leads(campaign_id, status, next_at);
CREATE TABLE IF NOT EXISTS sip_trunks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    vendor      TEXT NOT NULL DEFAULT 'custom',     -- twilio | telnyx | plivo | signalwire | vonage | voipms | custom …
    host        TEXT NOT NULL,                      -- the vendor's SIP server / termination URI host
    port        INTEGER NOT NULL DEFAULT 5060,
    username    TEXT NOT NULL DEFAULT '',
    password    TEXT NOT NULL DEFAULT '',           -- encrypted (vault.encrypt)
    from_user   TEXT NOT NULL DEFAULT '',
    from_domain TEXT NOT NULL DEFAULT '',
    register    INTEGER NOT NULL DEFAULT 0,         -- send REGISTER (vendors that deliver incoming calls to a registration)
    inbound_ips TEXT NOT NULL DEFAULT '',           -- IPs / CIDRs the vendor sends incoming calls from, one per line
    dial_format TEXT NOT NULL DEFAULT 'e164',       -- e164 (+15551234567) | digits (15551234567)
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS phone_numbers (
    id          INTEGER PRIMARY KEY,
    number      TEXT NOT NULL UNIQUE,               -- E.164
    label       TEXT NOT NULL DEFAULT '',
    trunk_id    INTEGER REFERENCES sip_trunks(id) ON DELETE SET NULL,   -- outgoing calls with this caller ID use it
    inbound     TEXT NOT NULL DEFAULT 'agents',     -- calls arriving over the trunk: agents | reject
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
"""

# Columns added after the first release: (table, column, definition)
MIGRATIONS = [
    ("contacts", "dnc", "INTEGER NOT NULL DEFAULT 0"),
    ("contacts", "score", "INTEGER"),
    ("contacts", "custom", "TEXT NOT NULL DEFAULT '{}'"),
    ("calls", "campaign_id", "INTEGER"),
    ("calls", "lead_id", "INTEGER"),
    ("calls", "ai_agent_id", "INTEGER"),
    ("calls", "provider", "TEXT NOT NULL DEFAULT 'asterisk'"),   # asterisk | telnyx | twilio | elevenlabs
    ("calls", "external_id", "TEXT NOT NULL DEFAULT ''"),
    ("calls", "recording", "TEXT NOT NULL DEFAULT ''"),
    ("calls", "transcript", "TEXT NOT NULL DEFAULT ''"),          # JSON [{role, text}]
    ("calls", "summary", "TEXT NOT NULL DEFAULT ''"),
    ("calls", "sentiment", "TEXT NOT NULL DEFAULT ''"),
    ("calls", "score", "INTEGER"),
    ("calls", "next_step", "TEXT NOT NULL DEFAULT ''"),
    ("calls", "callback_at", "INTEGER"),
    ("calls", "ai_fields", "TEXT NOT NULL DEFAULT '{}'"),
    ("calls", "amd", "TEXT NOT NULL DEFAULT ''"),
    ("calls", "analysis", "TEXT NOT NULL DEFAULT ''"),            # '' | pending | done | error: …
]

DEFAULT_SETTINGS = {
    "company_name": "IntelReach CRM",
    "twilio_number": "",            # +1XXXXXXXXXX – caller ID for outgoing calls
    "twilio_number_sid": "",        # PN… of that number
    "ghl_voice_url": "",            # GHL's webhook, saved when the number is connected
    "ghl_voice_method": "POST",
    "ghl_voice_app_sid": "",        # set instead of the URL if GHL used a TwiML App
    "inbound_mode": "crm_then_ghl", # crm_then_ghl | crm_only | ghl_only
    "ring_timeout": "20",           # seconds CRM agents ring before GHL takes over
    "default_country": "+1",        # added to numbers typed without +/00
    "allowed_prefixes": "*",        # e.g. "+1,+44,+92" – * = every country Twilio allows
    "blocked_prefixes": "",
    "max_call_minutes": "60",
    # recording / AI on human calls
    "record_calls": "0",            # Asterisk MixMonitor on CRM calls (check local consent laws)
    "transcribe_recordings": "1",   # AssemblyAI transcript of every recording
    "live_captions": "0",           # agents see live captions + AI tips (AssemblyAI streaming)
    "analyze_calls": "1",           # LLM summary / outcome / score after each call with a transcript
    "analysis_llm": "anthropic",    # provider for summaries & tips
    "analysis_model": "claude-opus-5-5",
    # AI answering calls to the Twilio number
    "inbound_ai_agent": "",         # ai_agents.id used by inbound modes with AI
    "telnyx_inbound_agent": "",     # ai_agents.id answering calls to Telnyx numbers ('' = reject)
    # which SIP trunk agent calls leave through (campaigns can override)
    "default_trunk": "",            # sip_trunks.id – '' = the first enabled trunk
}


def connect():
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def tx():
    con = connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init():
    with tx() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        for table, col, ddl in MIGRATIONS:
            have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        con.execute("CREATE INDEX IF NOT EXISTS calls_campaign ON calls(campaign_id)")
        con.execute("CREATE INDEX IF NOT EXISTS calls_external ON calls(external_id)")
        for k, v in DEFAULT_SETTINGS.items():
            con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    load_pool()


def load_pool():
    """Import agent SIP extensions written by scripts/gen_agent_pool.py."""
    if not os.path.exists(config.POOL_FILE):
        return 0
    with open(config.POOL_FILE, encoding="utf-8") as f:
        pool = json.load(f)
    with tx() as con:
        for p in pool:
            con.execute(
                "INSERT INTO sip_pool(ext, password) VALUES (?, ?) "
                "ON CONFLICT(ext) DO UPDATE SET password=excluded.password",
                (str(p["ext"]), p["password"]),
            )
    return len(pool)


def get_settings(con):
    return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")}


def set_setting(con, key, value):
    con.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def jload(text, default=None):
    try:
        return json.loads(text) if text else (default if default is not None else {})
    except ValueError:
        return default if default is not None else {}


def row(r):
    return dict(r) if r is not None else None


def rows(rs):
    return [dict(r) for r in rs]
