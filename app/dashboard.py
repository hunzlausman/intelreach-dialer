"""Dashboard: KPIs, daily trend, outcomes, campaign / agent leaderboards and live activity."""
import time

from fastapi import APIRouter, Depends

from . import config, db
from .security import current_user

router = APIRouter(prefix="/api")

POSITIVE = ("interested", "sale", "callback")


def _kpis(con, since, until, scope, args):
    r = con.execute(
        f"""SELECT COUNT(*) AS calls,
                   COALESCE(SUM(status = 'answered'), 0) AS connected,
                   COALESCE(SUM(direction = 'in'), 0) AS inbound,
                   COALESCE(SUM(direction = 'out'), 0) AS outbound,
                   COALESCE(SUM(ai_agent_id IS NOT NULL), 0) AS ai_calls,
                   COALESCE(SUM(status IN ('missed', 'no-agents')), 0) AS missed,
                   COALESCE(SUM(disposition IN {POSITIVE}), 0) AS positive,
                   COALESCE(SUM(duration), 0) AS talk_seconds,
                   AVG(score) AS avg_score
            FROM calls k WHERE started_at >= ? AND started_at < ?{scope}""", [since, until, *args]).fetchone()
    k = {key: r[key] for key in r.keys()}
    k["human_calls"] = k["calls"] - k["ai_calls"]
    k["connect_rate"] = round(100 * k["connected"] / k["calls"]) if k["calls"] else 0
    k["avg_talk"] = round(k["talk_seconds"] / k["connected"]) if k["connected"] else 0
    k["avg_score"] = round(k["avg_score"]) if k["avg_score"] is not None else None
    return k


@router.get("/dashboard")
def dashboard(days: int = 7, tz: int = 0, user=Depends(current_user)):
    """days: 1 / 7 / 30 / 90. tz: the browser's offset from UTC in minutes (so a "day" is the viewer's day)."""
    days = days if days in (1, 7, 30, 90) else 7
    now = int(time.time())
    off = max(-14 * 60, min(14 * 60, int(tz))) * 60
    # start of the viewer's day, `days` days ago
    today0 = (now + off) // 86400 * 86400 - off
    since = today0 - (days - 1) * 86400
    prev_since = since - days * 86400
    scope, args = "", []
    if user["role"] != "admin":
        scope, args = " AND (k.agent_id = ? OR (k.direction = 'in' AND k.agent_id IS NULL))", [user["id"]]
    with db.tx() as con:
        kpis = _kpis(con, since, now + 1, scope, args)
        prev = _kpis(con, prev_since, since, scope, args)
        rows = con.execute(
            f"""SELECT (started_at + ?) / 86400 AS d, COUNT(*) AS calls, COALESCE(SUM(status = 'answered'), 0) AS connected,
                       COALESCE(SUM(ai_agent_id IS NOT NULL), 0) AS ai
                FROM calls k WHERE started_at >= ?{scope} GROUP BY d""", [off, since, *args]).fetchall()
        by_day = {r["d"]: r for r in rows}
        first = (since + off) // 86400
        daily = []
        for i in range(days):
            r = by_day.get(first + i)
            daily.append({"day": time.strftime("%Y-%m-%d", time.gmtime((first + i) * 86400)),
                          "calls": r["calls"] if r else 0, "connected": r["connected"] if r else 0,
                          "ai": r["ai"] if r else 0})
        outcomes = db.rows(con.execute(
            f"""SELECT disposition AS outcome, COUNT(*) AS n FROM calls k
                WHERE started_at >= ? AND disposition != ''{scope} GROUP BY disposition ORDER BY n DESC""",
            [since, *args]))
        campaigns = db.rows(con.execute(
            f"""SELECT m.id, m.name, m.kind, m.status, COUNT(k.id) AS calls,
                       COALESCE(SUM(k.status = 'answered'), 0) AS connected,
                       COALESCE(SUM(k.disposition IN {POSITIVE}), 0) AS positive
                FROM campaigns m JOIN calls k ON k.campaign_id = m.id AND k.started_at >= ?{scope}
                GROUP BY m.id ORDER BY calls DESC LIMIT 8""", [since, *args]))
        agents = db.rows(con.execute(
            f"""SELECT COALESCE(a.name, u.name, 'Unassigned') AS name,
                       CASE WHEN k.ai_agent_id IS NOT NULL THEN 'ai' ELSE 'human' END AS type,
                       COUNT(*) AS calls, COALESCE(SUM(k.status = 'answered'), 0) AS connected,
                       COALESCE(SUM(k.duration), 0) AS talk_seconds,
                       COALESCE(SUM(k.disposition IN {POSITIVE}), 0) AS positive, AVG(k.score) AS avg_score
                FROM calls k LEFT JOIN users u ON u.id = k.agent_id LEFT JOIN ai_agents a ON a.id = k.ai_agent_id
                WHERE k.started_at >= ? AND (k.agent_id IS NOT NULL OR k.ai_agent_id IS NOT NULL){scope}
                GROUP BY type, COALESCE(k.ai_agent_id, k.agent_id) ORDER BY connected DESC, calls DESC LIMIT 10""",
            [since, *args]))
        for a in agents:
            a["avg_score"] = round(a["avg_score"]) if a["avg_score"] is not None else None
        live = db.rows(con.execute(
            f"""SELECT k.id, k.direction, k.number, k.status, k.started_at, c.name AS contact_name,
                       u.name AS agent_name, a.name AS ai_agent_name
                FROM calls k LEFT JOIN contacts c ON c.id = k.contact_id LEFT JOIN users u ON u.id = k.agent_id
                LEFT JOIN ai_agents a ON a.id = k.ai_agent_id
                WHERE k.ended_at IS NULL AND k.status IN ('dialing', 'ringing', 'answered') AND k.started_at > ?{scope}
                ORDER BY k.started_at DESC LIMIT 20""", [now - 4 * 3600, *args]))
        online = con.execute("SELECT COUNT(*) FROM users WHERE active = 1 AND available = 1 AND last_seen > ?",
                             (now - config.ONLINE_SECONDS,)).fetchone()[0]
        callbacks = db.rows(con.execute(
            f"""SELECT k.id, k.number, k.callback_at, k.next_step, c.name AS contact_name, k.contact_id
                FROM calls k LEFT JOIN contacts c ON c.id = k.contact_id
                WHERE k.callback_at IS NOT NULL AND k.callback_at > ?{scope} ORDER BY k.callback_at LIMIT 8""",
            [now - 3600, *args]))
    return {"days": days, "kpis": kpis, "prev": prev, "daily": daily, "outcomes": outcomes, "campaigns": campaigns,
            "agents": agents, "live": live, "online": online, "callbacks": callbacks}
