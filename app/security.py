"""Passwords, login sessions, Twilio webhook signatures, TURN credentials."""
import base64
import hashlib
import hmac
import secrets
import time

from fastapi import Cookie, Depends, HTTPException

from . import config, db, vault

COOKIE = "crm_session"


def hash_password(pw):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000)
    return f"pbkdf2${salt}${dk.hex()}"


def check_password(pw, stored):
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000)
    return hmac.compare_digest(dk.hex(), digest)


def new_session(con, user_id):
    token = secrets.token_urlsafe(32)
    con.execute("DELETE FROM sessions WHERE expires < ?", (int(time.time()),))
    con.execute(
        "INSERT INTO sessions(token, user_id, expires) VALUES (?, ?, ?)",
        (token, user_id, int(time.time()) + config.SESSION_DAYS * 86400),
    )
    return token


def current_user(crm_session: str = Cookie(default="")):
    if not crm_session:
        raise HTTPException(401, "Not signed in")
    with db.tx() as con:
        u = con.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ? AND s.expires > ? AND u.active = 1",
            (crm_session, int(time.time())),
        ).fetchone()
    if not u:
        raise HTTPException(401, "Session expired – sign in again")
    return dict(u)


def require_admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Admins only")
    return user


def twilio_signature_ok(url, params, signature):
    """https://www.twilio.com/docs/usage/security#validating-requests"""
    token = vault.twilio_creds()[1]
    if not token or not signature:
        return False
    data = url + "".join(k + params[k] for k in sorted(params))
    mac = hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), signature)


def ice_servers(user_id):
    if not config.TURN_SECRET:
        return [{"urls": f"stun:{config.TURN_HOST}:3478"}]
    username = f"{int(time.time()) + 6 * 3600}:crm{user_id}"
    cred = base64.b64encode(hmac.new(config.TURN_SECRET.encode(), username.encode(), hashlib.sha1).digest()).decode()
    h = config.TURN_HOST
    return [
        {"urls": f"stun:{h}:3478"},
        {"urls": [f"turn:{h}:3478?transport=udp", f"turn:{h}:3478?transport=tcp", f"turns:{h}:5349?transport=tcp"],
         "username": username, "credential": cred},
    ]
