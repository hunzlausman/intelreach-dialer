"""What happens when a call ends – for every kind of call (agent, AI, voicemail):
update the call log, move the campaign lead on (done / retry / failed), update the
contact, and queue the AI analysis (transcript -> summary, outcome, score)."""
import asyncio
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import db

log = logging.getLogger("crm.outcomes")
MAIN_LOOP = None            # set at startup; sync code (Asterisk hooks) schedules async work on it

ENDED_OK = {"answered"}
OUTCOME_SET = {"interested", "not-interested", "callback", "voicemail", "no-answer", "wrong-number", "sale", "other"}
FINAL_DISPOSITIONS = {"interested", "not-interested", "wrong-number", "sale", "other", "do-not-call"}


def spawn(coro):
    """Run a coroutine in the background from sync or async code."""
    try:
        loop = asyncio.get_running_loop()
        return loop.create_task(coro)
    except RuntimeError:
        if MAIN_LOOP and MAIN_LOOP.is_running():
            return asyncio.run_coroutine_threadsafe(coro, MAIN_LOOP)
        coro.close()


def parse_when(text, tz=""):
    """'2026-10-12T15:00' (contact's local time) -> unix time, or None."""
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(tz or "UTC"))
        except Exception:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return int(dt.timestamp())


def _campaign_cfg(con, campaign_id):
    c = con.execute("SELECT config FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    return db.jload(c["config"]) if c else {}


def update_lead(con, call):
    """Move the campaign lead on after its call ended."""
    if not call["lead_id"]:
        return
    lead = con.execute("SELECT * FROM campaign_leads WHERE id = ?", (call["lead_id"],)).fetchone()
    if not lead or lead["last_call_id"] != call["id"] or lead["status"] not in ("calling",):
        return
    cfg = _campaign_cfg(con, lead["campaign_id"])
    now = int(time.time())
    status, result, next_at = "done", call["disposition"] or call["status"], 0
    reached = call["status"] in ENDED_OK and call["amd"] != "machine"
    if call["disposition"] == "do-not-call":
        status = "dnc"
    elif call["disposition"] == "callback" or (reached and call["callback_at"]):
        status, next_at = "retry", call["callback_at"] or now + int(cfg.get("retry_minutes") or 60) * 60
    elif call["amd"] == "machine" and call["status"] == "voicemail-dropped":
        status = "done"
    elif not reached:
        if lead["attempts"] < int(cfg.get("max_attempts") or 3):
            status, next_at = "retry", now + int(cfg.get("retry_minutes") or 60) * 60
        else:
            status = "failed"
    con.execute("UPDATE campaign_leads SET status=?, result=?, next_at=?, agent_id=NULL, updated_at=? WHERE id=?",
                (status, result[:40], next_at, now, lead["id"]))


def finish_call(call_id, status, duration=None, **extra):
    """Final status of a call (idempotent: a later report never downgrades 'answered')."""
    with db.tx() as con:
        call = con.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        if not call:
            return
        if call["status"] == "answered" and status != "answered" and status != "voicemail-dropped":
            status = "answered"
        sets = {"status": status, "ended_at": call["ended_at"] or int(time.time())}
        if duration is not None:
            sets["duration"] = max(int(duration), call["duration"] or 0)
        sets.update({k: v for k, v in extra.items() if v is not None})
        con.execute(f"UPDATE calls SET {', '.join(k + '=?' for k in sets)} WHERE id=?", [*sets.values(), call_id])
        call = con.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        update_lead(con, call)
    maybe_analyze(call_id)


def apply_disposition(con, call_id, disposition):
    """Agent picked an outcome after the call (PATCH /api/calls/{id})."""
    call = con.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    if not call:
        return
    if call["contact_id"] and disposition == "do-not-call":
        con.execute("UPDATE contacts SET dnc = 1 WHERE id = ?", (call["contact_id"],))
        con.execute("UPDATE campaign_leads SET status='dnc' WHERE contact_id = ? AND status IN ('pending','retry')",
                    (call["contact_id"],))
    if not call["lead_id"]:
        return
    lead = con.execute("SELECT * FROM campaign_leads WHERE id = ?", (call["lead_id"],)).fetchone()
    if not lead or lead["last_call_id"] != call_id:
        return
    now = int(time.time())
    if disposition == "callback":
        cfg = _campaign_cfg(con, lead["campaign_id"])
        nxt = call["callback_at"] or now + int(cfg.get("retry_minutes") or 60) * 60
        con.execute("UPDATE campaign_leads SET status='retry', next_at=?, result=? WHERE id=?", (nxt, disposition, lead["id"]))
    elif disposition == "do-not-call":
        con.execute("UPDATE campaign_leads SET status='dnc', result=? WHERE id=?", (disposition, lead["id"]))
    elif disposition in FINAL_DISPOSITIONS or disposition in ("voicemail", "no-answer"):
        if lead["status"] in ("calling", "retry", "done", "failed"):
            st = "done" if disposition in FINAL_DISPOSITIONS else lead["status"]
            con.execute("UPDATE campaign_leads SET status=?, result=? WHERE id=?", (st, disposition, lead["id"]))


async def finish_ai_conversation(call_id, transcript, fields, outcome, callback, duration):
    """End of a custom-pipeline AI call: store what the AI learned."""
    tz = ""
    with db.tx() as con:
        call = con.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        if not call:
            return
        if call["lead_id"]:
            lead = con.execute("SELECT tz FROM campaign_leads WHERE id = ?", (call["lead_id"],)).fetchone()
            tz = lead["tz"] if lead else ""
    cb = parse_when(callback["when"], tz) if callback else None
    spoke = any(t["role"] == "contact" for t in transcript)
    finish_call(call_id, "answered" if spoke or transcript else call["status"], duration,
                transcript=json.dumps(transcript, ensure_ascii=False),
                ai_fields=json.dumps(fields, ensure_ascii=False), disposition=outcome or None, callback_at=cb,
                next_step=(callback or {}).get("note") or None)
    if outcome:
        with db.tx() as con:          # the carrier's hangup event may have moved the lead on already
            apply_disposition(con, call_id, outcome)
    if fields:
        merge_contact_fields(call_id, fields)


def merge_contact_fields(call_id, fields):
    with db.tx() as con:
        call = con.execute("SELECT contact_id FROM calls WHERE id = ?", (call_id,)).fetchone()
        if not call or not call["contact_id"]:
            return
        c = con.execute("SELECT custom, email FROM contacts WHERE id = ?", (call["contact_id"],)).fetchone()
        custom = db.jload(c["custom"])
        custom.update({k: v for k, v in fields.items() if v not in (None, "")})
        email = c["email"] or str(fields.get("email") or "")
        con.execute("UPDATE contacts SET custom = ?, email = ?, updated_at = strftime('%s','now') WHERE id = ?",
                    (json.dumps(custom, ensure_ascii=False), email[:200], call["contact_id"]))


def maybe_analyze(call_id):
    with db.tx() as con:
        s = db.get_settings(con)
        call = con.execute("SELECT transcript, recording, analysis, status FROM calls WHERE id = ?", (call_id,)).fetchone()
        if not call or call["analysis"] or s.get("analyze_calls") != "1":
            return
        has_text = bool(call["transcript"])
        can_transcribe = bool(call["recording"]) and s.get("transcribe_recordings") == "1"
        if call["status"] != "answered" or not (has_text or can_transcribe):
            return
        con.execute("UPDATE calls SET analysis = 'pending' WHERE id = ?", (call_id,))
    from .ai import analysis
    spawn(analysis.analyze_call(call_id))
