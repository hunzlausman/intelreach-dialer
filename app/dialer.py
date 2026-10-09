"""Background campaign dialer (runs inside the CRM process).

Every few seconds, for each running AI / voicemail campaign: fill the free call
slots (concurrency) with leads that are due and inside their local calling window.
Power-dialer campaigns are driven by agents (POST /api/campaigns/{id}/next); the
loop only cleans up their stale reservations and marks campaigns completed.
"""
import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import db, launcher

log = logging.getLogger("crm.dialer")
TICK = 3


def in_window(cfg, tz, now=None):
    """Is it calling time for this lead? Days 1=Mon … 7=Sun, hours in the lead's own time zone."""
    try:
        zone = ZoneInfo(tz or cfg.get("timezone") or "UTC")
    except Exception:
        zone = ZoneInfo("UTC")
    local = datetime.fromtimestamp(now or time.time(), zone)
    days = [int(d) for d in str(cfg.get("days") or "1,2,3,4,5").split(",") if d.strip().isdigit()]
    if days and local.isoweekday() not in days:
        return False
    start, end = cfg.get("window_start") or "09:00", cfg.get("window_end") or "18:00"
    hm = local.strftime("%H:%M")
    return start <= hm < end if start <= end else (hm >= start or hm < end)


def due_leads(con, campaign_id, cfg, limit, now=None, agent_id=None):
    """Leads ready to call now (pending or retry time reached), in their window, not DNC."""
    now = now or int(time.time())
    rows = con.execute(
        """SELECT l.*, c.name, c.phone, c.company, c.email, c.country, c.notes FROM campaign_leads l
           JOIN contacts c ON c.id = l.contact_id
           WHERE l.campaign_id = ? AND l.status IN ('pending', 'retry') AND l.next_at <= ? AND c.dnc = 0
           ORDER BY l.next_at, l.id LIMIT ?""", (campaign_id, now, max(limit * 5, 20))).fetchall()
    out = [r for r in rows if in_window(cfg, r["tz"], now)]
    return out[:limit]


def reserve(con, lead_id, agent_id=None):
    n = con.execute("UPDATE campaign_leads SET status='calling', agent_id=?, updated_at=? "
                    "WHERE id=? AND status IN ('pending','retry')", (agent_id, int(time.time()), lead_id)).rowcount
    return n == 1


def housekeeping(con, now):
    # power dialer: agent reserved a lead but never dialled it
    con.execute("UPDATE campaign_leads SET status = CASE WHEN attempts > 0 THEN 'retry' ELSE 'pending' END, agent_id = NULL "
                "WHERE status = 'calling' AND last_call_id IS NULL AND updated_at < ?", (now - 1800,))
    # call ended but the lead missed its update (crash, lost webhook)
    from .outcomes import update_lead
    for call in con.execute("""SELECT k.* FROM campaign_leads l JOIN calls k ON k.id = l.last_call_id
                               WHERE l.status = 'calling' AND k.ended_at IS NOT NULL AND k.ended_at < ?""", (now - 120,)):
        update_lead(con, call)
    # calls that never reported an end (carrier webhook lost) – give up after 3 hours
    con.execute("UPDATE calls SET status = 'failed', ended_at = ?, cause = 'timeout' "
                "WHERE ended_at IS NULL AND provider != 'asterisk' AND started_at < ?", (now, now - 3 * 3600))


async def tick():
    now = int(time.time())
    launches = []
    with db.tx() as con:
        housekeeping(con, now)
        for camp in con.execute("SELECT * FROM campaigns WHERE status = 'running'").fetchall():
            cfg = db.jload(camp["config"])
            open_leads = con.execute("SELECT COUNT(*) FROM campaign_leads WHERE campaign_id = ? AND status IN "
                                     "('pending','retry','calling')", (camp["id"],)).fetchone()[0]
            if not open_leads:
                con.execute("UPDATE campaigns SET status = 'completed' WHERE id = ?", (camp["id"],))
                continue
            if camp["kind"] == "power":
                continue
            active = con.execute("SELECT COUNT(*) FROM calls WHERE campaign_id = ? AND ended_at IS NULL",
                                 (camp["id"],)).fetchone()[0]
            slots = max(0, int(cfg.get("concurrency") or 1) - active)
            for lead in due_leads(con, camp["id"], cfg, slots, now) if slots else []:
                if reserve(con, lead["id"]):
                    launches.append((camp["id"], camp["kind"], cfg, dict(lead)))
    for camp_id, kind, cfg, lead in launches:
        contact = {"id": lead["contact_id"], "name": lead["name"], "phone": lead["phone"], "company": lead["company"],
                   "email": lead["email"], "country": lead["country"], "notes": lead["notes"]}
        try:
            if kind == "ai":
                await launcher.place_ai_call(int(cfg["ai_agent_id"]), lead["phone"], contact, camp_id, lead["id"],
                                             carrier=cfg.get("ai_carrier") or "sip", caller_id=cfg.get("from_number", ""))
            else:
                await launcher.place_voicemail(camp_id, cfg, contact, lead["id"])
        except Exception as e:
            log.warning("campaign %s lead %s: %s", camp_id, lead["id"], e)
            if "missing" in str(e).lower():          # configuration problem: stop instead of burning leads
                with db.tx() as con:
                    con.execute("UPDATE campaigns SET status = 'paused', config = json_set(config, '$.error', ?) "
                                "WHERE id = ?", (str(e)[:200], camp_id))


async def run_forever():
    while True:
        try:
            await tick()
        except Exception as e:
            log.exception("dialer tick failed: %s", e)
        await asyncio.sleep(TICK)
