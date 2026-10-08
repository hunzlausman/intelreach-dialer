"""SIP trunks (any vendor) and the phone numbers on them.

Admin → SIP trunks stores them in the database; apply() renders them into
pjsip_trunks.conf (included by /etc/asterisk/pjsip_crm.conf) and trunk_ips.txt.
The root unit intelreach-crm-trunks.path notices the change and runs
scripts/apply_trunks.sh: Asterisk reloads PJSIP, ufw opens 5060/udp to the IPs.

Agent call:  /ast/authorize -> route() picks trunk + caller ID -> Dial(PJSIP/<n>@crm-trunk-<id>)
Incoming:    vendor -> crm-trunk-<id> (identify by IP, or registration line) -> /ast/inbound
"""
import ipaddress
import logging
import os
import re

from fastapi import HTTPException

from . import config, phone, vault

log = logging.getLogger("crm.trunks")

# Presets only pre-fill the form – every value can be changed. Hosts in <…> are per-account.
VENDORS = {
    "twilio":     {"label": "Twilio Elastic SIP Trunking", "host": "<your-trunk>.pstn.twilio.com", "register": False,
                   "help": "Trunk → Termination: SIP URI + Credential List (and an IP ACL with this server). "
                           "Incoming: Origination URI sip:<this server's IP>:5060 – Twilio's IPs are already allowed."},
    "telnyx":     {"label": "Telnyx", "host": "sip.telnyx.com", "register": True,
                   "inbound_ips": "192.76.120.10\n64.16.250.10\n185.246.41.140\n185.246.41.141\n103.115.244.145\n103.115.244.146",
                   "help": "Voice → SIP Trunking → Credentials connection. Assign the numbers to that connection and "
                           "let its Outbound Voice Profile allow your destination countries."},
    "plivo":      {"label": "Plivo Zentrunk", "host": "<trunk-id>.zt.plivo.com", "register": False,
                   "help": "Outbound trunk → termination domain + credentials. Inbound trunk → URI sip:<this server's IP>:5060; "
                           "add Plivo's signalling IPs below."},
    "signalwire": {"label": "SignalWire", "host": "<space>.sip.signalwire.com", "register": False,
                   "help": "SIP endpoint username/password from your Space. Add SignalWire's signalling IPs below for incoming calls."},
    "vonage":     {"label": "Vonage (Nexmo) SIP", "host": "sip.nexmo.com", "register": False,
                   "help": "Username = API key, password = API secret. Incoming: point the number at sip:<number>@<this server's IP>."},
    "voipms":     {"label": "VoIP.ms", "host": "<city>.voip.ms", "register": True, "dial_format": "digits",
                   "help": "Sub-account username/password; set the DID's routing to that sub-account."},
    "custom":     {"label": "Other SIP provider", "host": "", "register": False,
                   "help": "Any SIP trunk that takes username/password or IP authentication over UDP."},
}
INBOUND = {"agents", "reject"}
DIAL_FORMATS = {"e164", "digits"}
FIELDS = ("name", "vendor", "host", "port", "username", "password", "from_user", "from_domain", "register",
          "inbound_ips", "dial_format", "enabled")

HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
USER = re.compile(r"^[A-Za-z0-9_.+-]{0,128}$")


def endpoint(trunk_id):
    return f"crm-trunk-{int(trunk_id)}"


# ------------------------------------------------------------ validation ----

def _ips(text):
    out = []
    for item in re.split(r"[\s,;]+", text or ""):
        if not item:
            continue
        try:
            net = ipaddress.IPv4Network(item, strict=False)
        except ValueError:
            raise HTTPException(400, f"'{item}' is not an IPv4 address or range (e.g. 192.0.2.10 or 192.0.2.0/24)")
        if net.prefixlen < 8:
            raise HTTPException(400, f"{item} is far too wide a range")
        out.append(str(net) if net.prefixlen < 32 else str(net.network_address))
    return "\n".join(dict.fromkeys(out))


def clean(body, current=None):
    """Validated column values. Everything here ends up in Asterisk's config, so it is strict."""
    cur = dict(current or {})
    v = {k: body[k] for k in FIELDS if k in body}
    t = {**cur, **v}
    vendor = str(t.get("vendor") or "custom")
    if vendor not in VENDORS:
        raise HTTPException(400, "Unknown vendor")
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(t.get("name") or "")).strip()[:60] or VENDORS[vendor]["label"]
    host = str(t.get("host") or "").strip().lower()
    if not HOST.match(host):
        raise HTTPException(400, "SIP server must be a host name or IP, e.g. mytrunk.pstn.twilio.com")
    try:
        port = int(t.get("port") or 5060)
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        raise HTTPException(400, "Port must be 1–65535")
    username, from_user = str(t.get("username") or "").strip(), str(t.get("from_user") or "").strip()
    if not USER.match(username) or not USER.match(from_user):
        raise HTTPException(400, "Usernames may only contain letters, digits and . _ + -")
    from_domain = str(t.get("from_domain") or "").strip().lower()
    if from_domain and not HOST.match(from_domain):
        raise HTTPException(400, "From domain must be a host name")
    out = {"name": name, "vendor": vendor, "host": host, "port": port, "username": username, "from_user": from_user,
           "from_domain": from_domain, "register": int(str(t.get("register")) in ("1", "True", "true", "on")),
           "inbound_ips": "" if vendor == "twilio" else _ips(t.get("inbound_ips")),
           "dial_format": t.get("dial_format") or "e164",
           "enabled": int(str(t.get("enabled", 1)) in ("1", "True", "true", "on"))}
    if out["dial_format"] not in DIAL_FORMATS:
        raise HTTPException(400, "Number format must be e164 or digits")
    if "password" in v and str(v["password"] or ""):          # empty = keep the saved one
        pw = str(v["password"])
        if len(pw) > 128 or re.search(r"[\x00-\x1f\x7f\\]", pw):
            raise HTTPException(400, "Password: at most 128 characters, no backslash or control characters")
        out["password"] = vault.encrypt(pw)
    elif not current:
        out["password"] = ""
    if out["register"] and not (username and (out.get("password") or cur.get("password"))):
        raise HTTPException(400, "Registration needs a username and password")
    return out


def public(t):
    d = dict(t)
    d["password"] = "•••• set" if t["password"] else ""
    d["endpoint"] = endpoint(t["id"])
    return d


def clean_number(con, body):
    num = phone.normalize(str(body.get("number") or ""), "")
    if not num:
        raise HTTPException(400, "Number must look like +15551234567")
    trunk_id = body.get("trunk_id") or None
    if trunk_id is not None:
        if not str(trunk_id).isdigit() or not con.execute("SELECT 1 FROM sip_trunks WHERE id = ?", (int(trunk_id),)).fetchone():
            raise HTTPException(400, "Choose one of your SIP trunks")
        trunk_id = int(trunk_id)
    inbound = body.get("inbound") or "agents"
    if inbound not in INBOUND:
        raise HTTPException(400, "Incoming must be agents or reject")
    label = re.sub(r"[\x00-\x1f\x7f]", "", str(body.get("label") or "")).strip()[:60]
    return {"number": num, "label": label, "trunk_id": trunk_id, "inbound": inbound}


# ------------------------------------------------------------- routing ----

def find(con, ref):
    """A trunk by id, or (old settings / campaigns) by vendor name 'twilio' / 'telnyx'. Enabled only."""
    ref = str(ref or "").strip()
    if ref.isdigit():
        return con.execute("SELECT * FROM sip_trunks WHERE id = ? AND enabled = 1", (int(ref),)).fetchone()
    if ref in VENDORS:
        return con.execute("SELECT * FROM sip_trunks WHERE vendor = ? AND enabled = 1 ORDER BY id LIMIT 1", (ref,)).fetchone()
    return None


def route(con, settings, camp=None):
    """(trunk row or None, caller ID) for an agent call.
    Trunk: campaign's → the trunk of the campaign's caller ID number → default → first enabled.
    Caller ID: campaign's → first number on that trunk → the connected Twilio number."""
    camp = camp or {}
    caller = str(camp.get("caller_id") or "").strip()
    t = find(con, camp.get("trunk"))
    if not t and caller:
        n = con.execute("SELECT trunk_id FROM phone_numbers WHERE number = ?", (caller,)).fetchone()
        t = find(con, n["trunk_id"]) if n else None
    t = t or find(con, settings.get("default_trunk")) or \
        con.execute("SELECT * FROM sip_trunks WHERE enabled = 1 ORDER BY id LIMIT 1").fetchone()
    if t and not caller:
        n = con.execute("SELECT number FROM phone_numbers WHERE trunk_id = ? ORDER BY id LIMIT 1", (t["id"],)).fetchone()
        caller = n["number"] if n else ""
    return t, caller or settings.get("twilio_number", "")


def dial_number(t, number):
    return number.lstrip("+") if t["dial_format"] == "digits" else number


def inbound_number(con, trunk_ref, candidates, default_country):
    """Which of our numbers an incoming trunk call is for (Request-URI user, then the To header)."""
    nums = []
    for raw in candidates:
        m = re.search(r"sips?:([^@;>]+)@", raw or "")
        raw = m.group(1) if m else (raw or "")
        digits = re.sub(r"\D", "", raw)
        if digits:
            nums += ["+" + digits, phone.normalize(raw, default_country)]
    nums = [n for n in dict.fromkeys(nums) if n]
    if nums:
        r = con.execute(f"SELECT * FROM phone_numbers WHERE number IN ({','.join('?' * len(nums))}) ORDER BY id LIMIT 1",
                        nums).fetchone()
        if r:
            return r
    # registration-style vendors may only put our username in the request: a trunk with one number is unambiguous
    if str(trunk_ref).isdigit():
        rs = con.execute("SELECT * FROM phone_numbers WHERE trunk_id = ? LIMIT 2", (int(trunk_ref),)).fetchall()
        if len(rs) == 1:
            return rs[0]
    return None


# --------------------------------------------------------------- Asterisk ----

def _val(s):
    return str(s).replace(";", r"\;")          # ';' starts a comment in Asterisk config files


def render(con):
    """pjsip_trunks.conf text + the firewall IP list."""
    out = ["; Generated by the IntelReach CRM (Admin → SIP trunks) – edits here are overwritten.", ""]
    ips = []
    for t in con.execute("SELECT * FROM sip_trunks WHERE enabled = 1 ORDER BY id"):
        ep, host = endpoint(t["id"]), t["host"]
        hostport = host if t["port"] == 5060 else f"{host}:{t['port']}"
        pw = vault.decrypt(t["password"])
        auth = bool(t["username"] and pw)
        out += [f"; ---- {t['name']} ({t['vendor']}) ----",
                f"[{ep}]", "type=endpoint", "transport=transport-udp-crm", "context=crm-from-trunk",
                "disallow=all", "allow=ulaw,alaw", "direct_media=no", "rtp_symmetric=yes", "force_rport=yes",
                "rewrite_contact=yes", "dtmf_mode=rfc4733", f"aors={ep}", f"from_domain={t['from_domain'] or host}",
                "send_pai=yes", "trust_id_inbound=yes", "rtp_timeout=60", f"set_var=CRM_TRUNK={t['id']}"]
        if t["from_user"]:
            out.append(f"from_user={t['from_user']}")
        if auth:
            out += [f"outbound_auth={ep}-auth", "", f"[{ep}-auth]", "type=auth", "auth_type=userpass",
                    f"username={t['username']}", f"password={_val(pw)}"]
        out += ["", f"[{ep}]", "type=aor", f"contact=sip:{hostport}", "qualify_frequency=60", ""]
        nets = (t["inbound_ips"] or "").split()
        if nets:
            out += [f"[{ep}-identify]", "type=identify", f"endpoint={ep}"] + [f"match={n}" for n in nets] + [""]
            ips += nets
        if t["register"] and auth:
            out += [f"[{ep}-reg]", "type=registration", "transport=transport-udp-crm", f"outbound_auth={ep}-auth",
                    f"server_uri=sip:{hostport}", f"client_uri=sip:{t['username']}@{hostport}",
                    f"contact_user={t['username']}", "retry_interval=60", "forbidden_retry_interval=600",
                    "expiration=3600", "line=yes", f"endpoint={ep}", ""]
    return "\n".join(out) + "\n", "\n".join(dict.fromkeys(ips)) + "\n"


def _write(path, text):
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)
    return True


def apply(con):
    """Write the Asterisk files. Returns '' or why it could not (the trunks are saved either way)."""
    conf, ips = render(con)
    try:
        os.makedirs(config.TRUNKS_DIR, exist_ok=True)
        _write(os.path.join(config.TRUNKS_DIR, "trunk_ips.txt"), ips)          # first: the .conf change triggers the reload
        _write(os.path.join(config.TRUNKS_DIR, "pjsip_trunks.conf"), conf)
        return ""
    except OSError as e:
        log.warning("could not write the trunk config: %s", e)
        return f"Saved, but Asterisk was not updated ({e}) – re-run deploy/install.sh"


def seed_from_env(con):
    """One-time move of the old .env trunks (TWILIO_SIP_*, TELNYX_SIP_*) into Admin → SIP trunks."""
    from . import db
    st = db.get_settings(con)
    if st.get("trunks_seeded") == "1":
        return
    db.set_setting(con, "trunks_seeded", "1")
    if con.execute("SELECT 1 FROM sip_trunks").fetchone():
        return
    env = config.env
    made = {}
    for vendor, host, user, pw in (("twilio", env("TWILIO_TERMINATION"), env("TWILIO_SIP_USER"), env("TWILIO_SIP_PASS")),
                                   ("telnyx", env("TELNYX_SIP_HOST", "sip.telnyx.com"), env("TELNYX_SIP_USER"),
                                    env("TELNYX_SIP_PASS"))):
        if not (host and user):
            continue
        made[vendor] = con.execute(
            "INSERT INTO sip_trunks(name, vendor, host, username, password) VALUES (?, ?, ?, ?, ?)",
            (VENDORS[vendor]["label"], vendor, host.lower(), user, vault.encrypt(pw))).lastrowid
    if st.get("default_trunk") in made:
        db.set_setting(con, "default_trunk", made[st["default_trunk"]])
    elif st.get("default_trunk") in VENDORS:
        db.set_setting(con, "default_trunk", "")
    cid = st.get("telnyx_trunk_caller_id")
    if cid and "telnyx" in made:
        con.execute("INSERT OR IGNORE INTO phone_numbers(number, label, trunk_id) VALUES (?, 'Telnyx caller ID', ?)",
                    (cid, made["telnyx"]))
    if made:
        log.info("moved SIP trunks from the .env into the database: %s", ", ".join(made))
