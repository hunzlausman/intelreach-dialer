"""Campaigns: power dialer (human agents), AI agent calling, voicemail drop."""
import json
import re
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import api, db, dialer, launcher, phone
from .security import current_user, require_admin

router = APIRouter(prefix="/api/campaigns")

KINDS = {"power", "ai", "voicemail"}
CONFIG_KEYS = {
    "goal", "script", "ai_agent_id", "from_number", "concurrency", "max_attempts", "retry_minutes",
    "window_start", "window_end", "days", "timezone", "preview_seconds", "vm_text", "vm_tts", "vm_voice_id",
    "vm_language", "on_human", "transfer_to", "trunk", "caller_id", "ai_carrier",
}


def stats(con, cid):
    leads = {r["status"]: r["n"] for r in con.execute(
        "SELECT status, COUNT(*) AS n FROM campaign_leads WHERE campaign_id = ? GROUP BY status", (cid,))}
    calls = con.execute(
        """SELECT COUNT(*) AS calls, SUM(status = 'answered') AS answered, SUM(status = 'voicemail-dropped') AS voicemails,
                  COALESCE(SUM(duration), 0) AS seconds, SUM(ended_at IS NULL) AS live,
                  SUM(disposition IN ('interested', 'sale')) AS positive
           FROM calls WHERE campaign_id = ?""", (cid,)).fetchone()
    return {"leads": leads, "total": sum(leads.values()), **{k: calls[k] or 0 for k in calls.keys()}}


def out(con, c):
    d = dict(c)
    d["config"] = db.jload(c["config"])
    d["stats"] = stats(con, c["id"])
    return d


def clean_config(kind, cfg):
    cfg = {k: v for k, v in (cfg or {}).items() if k in CONFIG_KEYS}
    for k in ("concurrency", "max_attempts", "retry_minutes", "preview_seconds"):
        if k in cfg and cfg[k] not in ("", None):
            try:
                cfg[k] = max(0, int(cfg[k]))
            except (TypeError, ValueError):
                raise HTTPException(400, f"{k} must be a number")
    cfg["concurrency"] = min(int(cfg.get("concurrency") or 1), 50)
    if cfg.get("ai_carrier", "sip") not in ("sip", "agent", "telnyx", "twilio"):
        raise HTTPException(400, "Calls go out through: sip, telnyx, twilio or the agent's own setting")
    if cfg.get("from_number") and not phone.E164.match(str(cfg["from_number"])):
        raise HTTPException(400, "Caller ID must look like +15551234567")
    for k in ("window_start", "window_end"):
        if cfg.get(k) and not re.match(r"^\d{2}:\d{2}$", str(cfg[k])):
            raise HTTPException(400, f"{k} must look like 09:00")
    if cfg.get("timezone"):
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(cfg["timezone"])
        except Exception:
            raise HTTPException(400, "Unknown time zone (use e.g. America/New_York, Asia/Karachi)")
    return cfg


class CampaignIn(BaseModel):
    name: str
    kind: str
    config: dict = {}


@router.get("")
def list_campaigns(user=Depends(current_user)):
    with db.tx() as con:
        return {"items": [out(con, c) for c in con.execute("SELECT * FROM campaigns ORDER BY id DESC")]}


@router.post("")
def create_campaign(body: CampaignIn, user=Depends(require_admin)):
    if body.kind not in KINDS:
        raise HTTPException(400, "Kind must be power, ai or voicemail")
    with db.tx() as con:
        cid = con.execute("INSERT INTO campaigns(name, kind, config, created_by) VALUES (?, ?, ?, ?)",
                          (body.name.strip()[:120] or "Campaign", body.kind,
                           json.dumps(clean_config(body.kind, body.config)), user["id"])).lastrowid
        return out(con, con.execute("SELECT * FROM campaigns WHERE id = ?", (cid,)).fetchone())


def _get(con, cid):
    c = con.execute("SELECT * FROM campaigns WHERE id = ?", (cid,)).fetchone()
    if not c:
        raise HTTPException(404, "Campaign not found")
    return c


@router.get("/{cid}")
def get_campaign(cid: int, user=Depends(current_user)):
    with db.tx() as con:
        return out(con, _get(con, cid))


@router.put("/{cid}")
def update_campaign(cid: int, body: CampaignIn, user=Depends(require_admin)):
    with db.tx() as con:
        c = _get(con, cid)
        con.execute("UPDATE campaigns SET name = ?, config = ? WHERE id = ?",
                    (body.name.strip()[:120] or c["name"], json.dumps(clean_config(c["kind"], body.config)), cid))
        return out(con, _get(con, cid))


@router.delete("/{cid}")
def delete_campaign(cid: int, user=Depends(require_admin)):
    with db.tx() as con:
        if con.execute("SELECT COUNT(*) FROM calls WHERE campaign_id = ? AND ended_at IS NULL", (cid,)).fetchone()[0]:
            raise HTTPException(409, "Calls are still running – pause the campaign and wait")
        con.execute("DELETE FROM campaigns WHERE id = ?", (cid,))
    return {"ok": True}


class StatusIn(BaseModel):
    status: str


@router.post("/{cid}/status")
async def set_status(cid: int, body: StatusIn, user=Depends(require_admin)):
    if body.status not in ("running", "paused"):
        raise HTTPException(400, "Status must be running or paused")
    with db.tx() as con:
        c = _get(con, cid)
        cfg = db.jload(c["config"])
        has_leads = con.execute("SELECT COUNT(*) FROM campaign_leads WHERE campaign_id = ? AND status IN "
                                "('pending','retry','calling')", (cid,)).fetchone()[0]
    if body.status == "running":
        if not has_leads:
            raise HTTPException(400, "Add leads first (or reset failed ones)")
        if c["kind"] == "ai" and not cfg.get("ai_agent_id"):
            raise HTTPException(400, "Choose the AI agent that makes the calls")
        if c["kind"] == "ai":
            with db.tx() as con:
                try:
                    problem = launcher.route_problem(con, launcher.load_agent(con, int(cfg["ai_agent_id"])),
                                                     cfg.get("ai_carrier") or "sip")
                except Exception as e:
                    problem = str(e)
            if problem:
                raise HTTPException(400, problem)
        if c["kind"] == "voicemail":
            if not cfg.get("vm_text"):
                raise HTTPException(400, "Write the voicemail message")
            if cfg.get("vm_tts") == "elevenlabs":
                try:
                    await launcher.build_voicemail_audio(cid, cfg)
                except Exception as e:
                    raise HTTPException(502, f"Could not create the ElevenLabs audio: {e}")
        cfg.pop("error", None)
    with db.tx() as con:
        con.execute("UPDATE campaigns SET status = ?, config = ? WHERE id = ?", (body.status, json.dumps(cfg), cid))
        return out(con, _get(con, cid))


# ----------------------------------------------------------------- leads ----

class LeadsIn(BaseModel):
    contact_ids: list[int] = []
    q: str = ""
    status: str = ""
    tag: str = ""
    all_matching: bool = False
    csv: str = ""


@router.get("/{cid}/leads")
def list_leads(cid: int, status: str = "", limit: int = 50, offset: int = 0, user=Depends(current_user)):
    where, args = "l.campaign_id = ?", [cid]
    if status:
        where += " AND l.status = ?"
        args.append(status)
    with db.tx() as con:
        total = con.execute(f"SELECT COUNT(*) FROM campaign_leads l WHERE {where}", args).fetchone()[0]
        items = db.rows(con.execute(
            f"""SELECT l.*, c.name, c.phone, c.company, u.name AS agent_name FROM campaign_leads l
                JOIN contacts c ON c.id = l.contact_id LEFT JOIN users u ON u.id = l.agent_id
                WHERE {where} ORDER BY l.id LIMIT ? OFFSET ?""", args + [max(1, min(limit, 200)), max(0, offset)]))
    return {"total": total, "items": items}


@router.post("/{cid}/leads")
def add_leads(cid: int, body: LeadsIn, user=Depends(require_admin)):
    ids = list(body.contact_ids)
    imported = None
    if body.csv.strip():
        imported = api.import_contacts(api.CsvIn(csv=body.csv), user)
        ids += imported.pop("ids")
    if body.all_matching:
        where, args = ["dnc = 0"], []
        if body.q.strip():
            like = f"%{body.q.strip()}%"
            where.append("(name LIKE ? OR phone LIKE ? OR company LIKE ? OR tags LIKE ?)")
            args += [like] * 4
        if body.status:
            where.append("status = ?")
            args.append(body.status)
        if body.tag:
            where.append("(',' || REPLACE(tags, ' ', '') || ',') LIKE ?")
            args.append(f"%,{body.tag.strip()},%")
        with db.tx() as con:
            ids += [r["id"] for r in con.execute(f"SELECT id FROM contacts WHERE {' AND '.join(where)}", args)]
    added = 0
    with db.tx() as con:
        _get(con, cid)
        for contact_id in dict.fromkeys(ids):
            c = con.execute("SELECT phone, dnc FROM contacts WHERE id = ?", (contact_id,)).fetchone()
            if not c or c["dnc"]:
                continue
            added += con.execute("INSERT OR IGNORE INTO campaign_leads(campaign_id, contact_id, tz) VALUES (?, ?, ?)",
                                 (cid, contact_id, phone.timezone_for(c["phone"]))).rowcount
        if added:
            con.execute("UPDATE campaigns SET status = 'paused' WHERE id = ? AND status = 'completed'", (cid,))
    return {"added": added, "imported": imported}


@router.delete("/{cid}/leads/{lead_id}")
def remove_lead(cid: int, lead_id: int, user=Depends(require_admin)):
    with db.tx() as con:
        con.execute("DELETE FROM campaign_leads WHERE id = ? AND campaign_id = ? AND status != 'calling'", (lead_id, cid))
    return {"ok": True}


@router.post("/{cid}/leads/reset")
def reset_leads(cid: int, user=Depends(require_admin)):
    """Failed leads get another round of attempts."""
    with db.tx() as con:
        n = con.execute("UPDATE campaign_leads SET status = 'pending', attempts = 0, next_at = 0 "
                        "WHERE campaign_id = ? AND status = 'failed'", (cid,)).rowcount
        if n:
            con.execute("UPDATE campaigns SET status = 'paused' WHERE id = ? AND status = 'completed'", (cid,))
    return {"reset": n}


# ---------------------------------------------------------- power dialer ----

@router.post("/{cid}/next")
def next_lead(cid: int, user=Depends(current_user)):
    """The agent's next contact to call (reserved for them)."""
    with db.tx() as con:
        c = _get(con, cid)
        if c["kind"] != "power":
            raise HTTPException(400, "Only power-dialer campaigns are dialled by agents")
        if c["status"] != "running":
            raise HTTPException(409, "This campaign is not running")
        # give back anything this agent still holds without a call
        con.execute("UPDATE campaign_leads SET status = CASE WHEN attempts > 0 THEN 'retry' ELSE 'pending' END, "
                    "agent_id = NULL WHERE campaign_id = ? AND agent_id = ? AND status = 'calling' AND "
                    "(last_call_id IS NULL OR last_call_id IN (SELECT id FROM calls WHERE ended_at IS NOT NULL))",
                    (cid, user["id"]))
        cfg = db.jload(c["config"])
        for lead in dialer.due_leads(con, cid, cfg, 5):
            if dialer.reserve(con, lead["id"], user["id"]):
                con.execute("UPDATE campaign_leads SET last_call_id = NULL WHERE id = ?", (lead["id"],))
                contact = db.row(con.execute("SELECT * FROM contacts WHERE id = ?", (lead["contact_id"],)).fetchone())
                history = db.rows(con.execute(
                    "SELECT started_at, status, disposition, notes, summary FROM calls WHERE contact_id = ? "
                    "ORDER BY started_at DESC LIMIT 5", (contact["id"],)))
                vars_ = {"name": contact["name"], "first_name": (contact["name"] or "").split(" ")[0],
                         "company": contact["company"], "agent": user["name"]}
                script = re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(vars_.get(m.group(1), "")), cfg.get("script", ""))
                return {"lead": dict(lead), "contact": contact, "history": history, "script": script,
                        "preview_seconds": int(cfg.get("preview_seconds") if cfg.get("preview_seconds") not in (None, "") else 5)}
        left = con.execute("SELECT COUNT(*) FROM campaign_leads WHERE campaign_id = ? AND status IN ('pending','retry')",
                           (cid,)).fetchone()[0]
    return {"lead": None, "remaining": left,
            "message": "No lead is due right now (outside calling hours or waiting for retries)" if left else "All leads done"}


@router.post("/{cid}/skip/{lead_id}")
def skip_lead(cid: int, lead_id: int, user=Depends(current_user)):
    with db.tx() as con:
        con.execute("UPDATE campaign_leads SET status = CASE WHEN attempts > 0 THEN 'retry' ELSE 'pending' END, "
                    "agent_id = NULL, next_at = ? WHERE id = ? AND campaign_id = ? AND agent_id = ? AND status = 'calling'",
                    (int(time.time()) + 600, lead_id, cid, user["id"]))
    return {"ok": True}
