"""Browser API: login, phone credentials, contacts, call log."""
import csv
import io
import time

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from . import config, db, outcomes, phone, security, trunks
from .security import current_user

router = APIRouter(prefix="/api")

CONTACT_FIELDS = ("name", "phone", "email", "company", "country", "tags", "status", "notes", "dnc")
CONTACT_STATUSES = {"new", "contacted", "interested", "customer", "closed"}
DISPOSITIONS = {"", "interested", "not-interested", "callback", "voicemail", "no-answer", "wrong-number", "sale", "other",
                "do-not-call"}


# ------------------------------------------------------------------ auth ----

class Login(BaseModel):
    email: str
    password: str


@router.post("/login")
def login(body: Login, response: Response):
    with db.tx() as con:
        u = con.execute("SELECT * FROM users WHERE email = ? AND active = 1", (body.email.strip(),)).fetchone()
        if not u or not security.check_password(body.password, u["pw_hash"]):
            time.sleep(0.5)
            raise HTTPException(401, "Wrong email or password")
        token = security.new_session(con, u["id"])
    response.set_cookie(security.COOKIE, token, max_age=config.SESSION_DAYS * 86400, httponly=True,
                        secure=config.COOKIE_SECURE, samesite="lax")
    return {"ok": True}


@router.post("/logout")
def logout(response: Response, user=Depends(current_user)):
    with db.tx() as con:
        con.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        con.execute("UPDATE users SET last_seen = 0 WHERE id = ?", (user["id"],))
    response.delete_cookie(security.COOKIE)
    return {"ok": True}


def public_user(u):
    return {k: u[k] for k in ("id", "email", "name", "role", "sip_ext", "available")}


@router.get("/me")
def me(user=Depends(current_user)):
    with db.tx() as con:
        s = db.get_settings(con)
        caller_id = trunks.route(con, s)[1]
        sip = None
        if user["sip_ext"]:
            p = con.execute("SELECT password FROM sip_pool WHERE ext = ?", (user["sip_ext"],)).fetchone()
            if p:
                sip = {"ext": user["sip_ext"], "password": p["password"], "wss": config.SIP_WSS,
                       "domain": config.SIP_DOMAIN, "iceServers": security.ice_servers(user["id"])}
    return {
        "user": public_user(user),
        "sip": sip,
        "company": s["company_name"],
        "callerId": caller_id,
        "version": config.VERSION,
        "defaultCountry": s["default_country"],
        "liveCaptions": s.get("live_captions") == "1",
        "recording": s.get("record_calls") == "1",
    }


class Heartbeat(BaseModel):
    available: bool | None = None
    registered: bool = True


@router.post("/me/heartbeat")
def heartbeat(body: Heartbeat, user=Depends(current_user)):
    with db.tx() as con:
        if body.available is not None:
            con.execute("UPDATE users SET available = ? WHERE id = ?", (int(body.available), user["id"]))
        con.execute("UPDATE users SET last_seen = ? WHERE id = ?",
                    (int(time.time()) if body.registered else 0, user["id"]))
    return {"ok": True}


# -------------------------------------------------------------- contacts ----

class ContactIn(BaseModel):
    name: str = ""
    phone: str
    email: str = ""
    company: str = ""
    country: str = ""
    tags: str = ""
    status: str = "new"
    notes: str = ""
    dnc: bool = False


def clean_contact(con, body: ContactIn):
    s = db.get_settings(con)
    d = {k: (getattr(body, k) or "").strip()[:2000] for k in CONTACT_FIELDS if k != "dnc"}
    d["dnc"] = int(bool(body.dnc))
    d["phone"] = phone.normalize(body.phone, s["default_country"])
    if not d["phone"]:
        raise HTTPException(400, f"'{body.phone}' is not a valid phone number – use +<country code><number>")
    if d["status"] not in CONTACT_STATUSES:
        d["status"] = "new"
    return d


@router.get("/contacts")
def list_contacts(q: str = "", status: str = "", limit: int = 50, offset: int = 0, user=Depends(current_user)):
    where, args = ["1=1"], []
    if q.strip():
        like = f"%{q.strip()}%"
        where.append("(name LIKE ? OR phone LIKE ? OR email LIKE ? OR company LIKE ? OR tags LIKE ?)")
        args += [like] * 5
    if status:
        where.append("status = ?")
        args.append(status)
    sql = " AND ".join(where)
    with db.tx() as con:
        total = con.execute(f"SELECT COUNT(*) FROM contacts WHERE {sql}", args).fetchone()[0]
        items = db.rows(con.execute(
            f"""SELECT c.*, (SELECT MAX(started_at) FROM calls WHERE contact_id = c.id) AS last_call
                FROM contacts c WHERE {sql} ORDER BY c.updated_at DESC LIMIT ? OFFSET ?""",
            args + [max(1, min(limit, 200)), max(0, offset)]))
    return {"total": total, "items": items}


@router.get("/contacts/{cid}")
def get_contact(cid: int, user=Depends(current_user)):
    with db.tx() as con:
        c = db.row(con.execute("SELECT * FROM contacts WHERE id = ?", (cid,)).fetchone())
        if not c:
            raise HTTPException(404, "Contact not found")
        c["calls"] = db.rows(con.execute(
            """SELECT k.*, u.name AS agent_name FROM calls k LEFT JOIN users u ON u.id = k.agent_id
               WHERE k.contact_id = ? OR k.number = ? ORDER BY k.started_at DESC LIMIT 100""", (cid, c["phone"])))
    return c


@router.post("/contacts")
def create_contact(body: ContactIn, user=Depends(current_user)):
    with db.tx() as con:
        d = clean_contact(con, body)
        cid = con.execute(
            f"INSERT INTO contacts({','.join(CONTACT_FIELDS)}, owner_id) VALUES ({','.join('?' * len(CONTACT_FIELDS))}, ?)",
            [d[k] for k in CONTACT_FIELDS] + [user["id"]]).lastrowid
        # earlier calls with this number now belong to the contact
        con.execute("UPDATE calls SET contact_id = ? WHERE number = ? AND contact_id IS NULL", (cid, d["phone"]))
    return {"id": cid}


@router.put("/contacts/{cid}")
def update_contact(cid: int, body: ContactIn, user=Depends(current_user)):
    with db.tx() as con:
        d = clean_contact(con, body)
        n = con.execute(
            f"UPDATE contacts SET {','.join(k + '=?' for k in CONTACT_FIELDS)}, updated_at=strftime('%s','now') WHERE id=?",
            [d[k] for k in CONTACT_FIELDS] + [cid]).rowcount
        if not n:
            raise HTTPException(404, "Contact not found")
    return {"ok": True}


@router.delete("/contacts/{cid}")
def delete_contact(cid: int, user=Depends(current_user)):
    with db.tx() as con:
        con.execute("DELETE FROM contacts WHERE id = ?", (cid,))
    return {"ok": True}


class CsvIn(BaseModel):
    csv: str


@router.post("/contacts/import")
def import_contacts(body: CsvIn, user=Depends(current_user)):
    """CSV with a header row. Recognised columns: name / first name / last name,
    phone / mobile / number, email, company, country, tags, notes."""
    reader = csv.DictReader(io.StringIO(body.csv.lstrip("﻿")))
    if not reader.fieldnames:
        raise HTTPException(400, "Empty file")
    cols = {f.strip().lower(): f for f in reader.fieldnames}

    def col(r, *names):
        for n in names:
            if n in cols and (r.get(cols[n]) or "").strip():
                return r[cols[n]].strip()
        return ""

    added, skipped, errors, ids = 0, 0, [], []
    with db.tx() as con:
        s = db.get_settings(con)
        for i, r in enumerate(reader, start=2):
            raw = col(r, "phone", "phone number", "mobile", "number", "telephone", "tel")
            num = phone.normalize(raw, s["default_country"])
            if not num:
                if len(errors) < 20:
                    errors.append(f"Row {i}: invalid phone '{raw}'")
                skipped += 1
                continue
            dup = con.execute("SELECT id FROM contacts WHERE phone = ?", (num,)).fetchone()
            if dup:
                ids.append(dup["id"])
                skipped += 1
                continue
            name = col(r, "name", "full name") or " ".join(x for x in (col(r, "first name", "firstname"), col(r, "last name", "lastname")) if x)
            ids.append(con.execute(
                "INSERT INTO contacts(name, phone, email, company, country, tags, notes, owner_id) VALUES (?,?,?,?,?,?,?,?)",
                (name[:200], num, col(r, "email", "e-mail")[:200], col(r, "company", "organization")[:200],
                 col(r, "country")[:80], col(r, "tags", "tag")[:500], col(r, "notes", "note")[:2000], user["id"])).lastrowid)
            added += 1
    return {"added": added, "skipped": skipped, "errors": errors, "ids": ids}


@router.get("/lookup")
def lookup(number: str, user=Depends(current_user)):
    with db.tx() as con:
        s = db.get_settings(con)
        num = phone.normalize(number, s["default_country"])
        c = con.execute("SELECT id, name, company, status FROM contacts WHERE phone = ?", (num,)).fetchone() if num else None
    return {"number": num, "contact": db.row(c)}


# ----------------------------------------------------------------- calls ----

class NewCall(BaseModel):
    number: str
    contact_id: int | None = None
    campaign_id: int | None = None
    lead_id: int | None = None


@router.post("/calls")
def start_call(body: NewCall, user=Depends(current_user)):
    """Called by the dialer right before it sends the SIP INVITE. Checks the
    number against the dialling policy so the agent sees a clear reason."""
    if not user["sip_ext"]:
        raise HTTPException(409, "You have no phone line – ask an admin to assign one")
    with db.tx() as con:
        s = db.get_settings(con)
        num = phone.normalize(body.number, s["default_country"])
        if not num:
            raise HTTPException(400, f"'{body.number}' is not a valid phone number – use +<country code><number>")
        reason = phone.check_allowed(num, s)
        if reason:
            raise HTTPException(403, reason)
        cid = body.contact_id
        if not cid:
            c = con.execute("SELECT id FROM contacts WHERE phone = ?", (num,)).fetchone()
            cid = c["id"] if c else None
        if cid and con.execute("SELECT dnc FROM contacts WHERE id = ?", (cid,)).fetchone()["dnc"]:
            raise HTTPException(403, "This contact is on the do-not-call list")
        lead_id = None
        if body.lead_id:
            lead = con.execute("SELECT * FROM campaign_leads WHERE id = ? AND agent_id = ? AND status = 'calling'",
                               (body.lead_id, user["id"])).fetchone()
            if not lead:
                raise HTTPException(409, "This lead is no longer reserved for you – get the next one")
            lead_id = lead["id"]
        camp = {}
        if lead_id:
            r = con.execute("SELECT config FROM campaigns WHERE id = ?", (lead["campaign_id"],)).fetchone()
            camp = db.jload(r["config"]) if r else {}
        trunk, caller_id = trunks.route(con, s, camp)
        if not trunk:
            raise HTTPException(409, "No SIP trunk yet – an admin must add one (Admin → SIP trunks)")
        if not caller_id:
            raise HTTPException(409, "No caller ID yet – an admin must add a number (Admin → SIP trunks → Numbers)")
        call_id = con.execute(
            "INSERT INTO calls(direction, number, contact_id, agent_id, status, campaign_id, lead_id) "
            "VALUES ('out', ?, ?, ?, 'new', ?, ?)",
            (num, cid, user["id"], lead["campaign_id"] if lead_id else None, lead_id)).lastrowid
        if lead_id:
            con.execute("UPDATE campaign_leads SET last_call_id = ?, attempts = attempts + 1 WHERE id = ?",
                        (call_id, lead_id))
        if cid:
            con.execute("UPDATE contacts SET status = 'contacted', updated_at = strftime('%s','now') "
                        "WHERE id = ? AND status = 'new'", (cid,))
    return {"id": call_id, "number": num}


@router.get("/calls")
def list_calls(q: str = "", direction: str = "", status: str = "", mine: bool = False, campaign: int = 0,
               ai: bool = False, limit: int = 50, offset: int = 0, user=Depends(current_user)):
    where, args = ["1=1"], []
    if q.strip():
        like = f"%{q.strip()}%"
        where.append("(k.number LIKE ? OR c.name LIKE ? OR c.company LIKE ? OR k.notes LIKE ?)")
        args += [like] * 4
    if direction in ("in", "out"):
        where.append("k.direction = ?")
        args.append(direction)
    if status:
        where.append("k.status = ?")
        args.append(status)
    if campaign:
        where.append("k.campaign_id = ?")
        args.append(campaign)
    if ai:
        where.append("k.ai_agent_id IS NOT NULL")
    if mine or user["role"] != "admin":
        # agents see their own calls plus incoming calls nobody answered
        where.append("(k.agent_id = ? OR (k.direction = 'in' AND k.agent_id IS NULL))")
        args.append(user["id"])
    sql = " AND ".join(where)
    base = ("FROM calls k LEFT JOIN contacts c ON c.id = k.contact_id LEFT JOIN users u ON u.id = k.agent_id "
            "LEFT JOIN ai_agents a ON a.id = k.ai_agent_id LEFT JOIN campaigns m ON m.id = k.campaign_id")
    with db.tx() as con:
        total = con.execute(f"SELECT COUNT(*) {base} WHERE {sql}", args).fetchone()[0]
        items = db.rows(con.execute(
            "SELECT k.id, k.direction, k.number, k.contact_id, k.agent_id, k.status, k.duration, k.disposition, "
            "k.notes, k.started_at, k.summary, k.score, k.sentiment, k.provider, k.campaign_id, k.ai_agent_id, k.cause, "
            "k.recording != '' AS has_recording, k.transcript != '' AS has_transcript, "
            "c.name AS contact_name, c.company, u.name AS agent_name, a.name AS ai_agent_name, m.name AS campaign_name "
            f"{base} WHERE {sql} "
            "ORDER BY k.started_at DESC LIMIT ? OFFSET ?", args + [max(1, min(limit, 200)), max(0, offset)]))
    return {"total": total, "items": items}


@router.get("/calls/{call_id}")
def get_call(call_id: int, user=Depends(current_user)):
    with db.tx() as con:
        k = db.row(con.execute(
            """SELECT k.*, c.name AS contact_name, u.name AS agent_name, a.name AS ai_agent_name, m.name AS campaign_name
               FROM calls k LEFT JOIN contacts c ON c.id = k.contact_id LEFT JOIN users u ON u.id = k.agent_id
               LEFT JOIN ai_agents a ON a.id = k.ai_agent_id LEFT JOIN campaigns m ON m.id = k.campaign_id
               WHERE k.id = ?""", (call_id,)).fetchone())
    if not k:
        raise HTTPException(404, "Call not found")
    if user["role"] != "admin" and k["agent_id"] not in (None, user["id"]):
        raise HTTPException(403, "Not your call")
    k["transcript"] = db.jload(k["transcript"], [])
    k["ai_fields"] = db.jload(k["ai_fields"])
    k["has_recording"] = bool(k.pop("recording"))
    return k


class CallUpdate(BaseModel):
    disposition: str = ""
    notes: str = ""


@router.patch("/calls/{call_id}")
def update_call(call_id: int, body: CallUpdate, user=Depends(current_user)):
    if body.disposition not in DISPOSITIONS:
        raise HTTPException(400, "Unknown outcome")
    with db.tx() as con:
        k = con.execute("SELECT agent_id, contact_id FROM calls WHERE id = ?", (call_id,)).fetchone()
        if not k:
            raise HTTPException(404, "Call not found")
        if user["role"] != "admin" and k["agent_id"] not in (None, user["id"]):
            raise HTTPException(403, "Not your call")
        con.execute("UPDATE calls SET disposition = ?, notes = ? WHERE id = ?", (body.disposition, body.notes[:4000], call_id))
        outcomes.apply_disposition(con, call_id, body.disposition)
        if k["contact_id"] and body.disposition in ("interested", "sale"):
            con.execute("UPDATE contacts SET status = ?, updated_at = strftime('%s','now') WHERE id = ?",
                        ("customer" if body.disposition == "sale" else "interested", k["contact_id"]))
    return {"ok": True}


@router.get("/stats")
def stats(user=Depends(current_user)):
    day = int(time.time()) - 86400
    mine = "" if user["role"] == "admin" else f" AND agent_id = {int(user['id'])}"
    with db.tx() as con:
        r = con.execute(
            f"""SELECT COUNT(*) AS calls,
                       SUM(status = 'answered') AS answered,
                       SUM(direction = 'out') AS outgoing,
                       SUM(direction = 'in') AS incoming,
                       COALESCE(SUM(duration), 0) AS seconds
                FROM calls WHERE started_at > ?{mine}""", (day,)).fetchone()
        online = con.execute("SELECT COUNT(*) FROM users WHERE active = 1 AND available = 1 AND last_seen > ?",
                             (int(time.time()) - config.ONLINE_SECONDS,)).fetchone()[0]
    return {**{k: r[k] or 0 for k in r.keys()}, "agentsOnline": online}
