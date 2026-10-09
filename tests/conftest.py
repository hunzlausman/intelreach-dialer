"""Shared test environment: temporary database, fake secrets, no background dialer."""
import json
import os
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
POOL = Path(TMP) / "pool.json"
(Path(TMP) / "spool").mkdir()
POOL.write_text(json.dumps([{"ext": "2001", "password": "p1"}, {"ext": "2002", "password": "p2"}]))
os.environ.update({
    "CRM_DB": str(Path(TMP) / "crm.db"), "CRM_POOL_FILE": str(POOL), "CRM_AST_SECRET": "sek",
    "TWILIO_ACCOUNT_SID": "ACtest", "TWILIO_AUTH_TOKEN": "tok", "CRM_PUBLIC_URL": "https://crm.example.com",
    "CRM_COOKIE_SECURE": "0", "TURN_SECRET": "turnsecret", "CRM_RUN_DIALER": "0",
    "CRM_MEDIA": str(Path(TMP) / "media"), "CRM_RECORDINGS": str(Path(TMP) / "rec"),
    "CRM_TRUNKS_DIR": str(Path(TMP) / "trunks"),
    "CRM_AST_SPOOL": str(Path(TMP) / "spool"), "CRM_AUDIOSOCKET_PORT": "0",      # 0 = any free port
    # old .env trunks: moved into Admin → SIP trunks on first start (Twilio = trunk 1, Telnyx = trunk 2)
    "TWILIO_TERMINATION": "crm.pstn.twilio.com", "TWILIO_SIP_USER": "tw-user", "TWILIO_SIP_PASS": "tw-pass",
    "TELNYX_SIP_USER": "tx-user", "TELNYX_SIP_PASS": "tx-pass",
})
