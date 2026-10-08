"""After a call: transcript (AssemblyAI, if only a recording exists) -> LLM summary,
outcome, sentiment, lead score, next step, extracted fields -> call + contact."""
import json
import logging
import os

from .. import db, outcomes
from . import assemblyai, llm

log = logging.getLogger("crm.analysis")

SYSTEM = """You analyse sales / support phone calls for a CRM.
Return JSON with exactly these keys:
  "summary":    2-4 sentences, what happened and what was agreed
  "outcome":    one of interested, not-interested, callback, voicemail, no-answer, wrong-number, sale, other
  "sentiment":  positive | neutral | negative
  "score":      0-100, how likely this contact is to buy / convert
  "next_step":  one short sentence ('' if none)
  "callback":   ISO 8601 local date-time if a callback was agreed, else ''
  "fields":     object with facts learned about the contact (only facts actually said)"""


def transcript_text(items):
    return "\n".join(f"{t.get('role', '?')}: {t.get('text', '')}" for t in items)


async def analyze_call(call_id):
    try:
        await _analyze(call_id)
    except Exception as e:
        log.warning("analysis of call %s failed: %s", call_id, e)
        with db.tx() as con:
            con.execute("UPDATE calls SET analysis = ? WHERE id = ?", (f"error: {str(e)[:200]}", call_id))


async def _analyze(call_id):
    with db.tx() as con:
        s = db.get_settings(con)
        call = db.row(con.execute(
            """SELECT k.*, c.name AS contact_name, c.company, c.custom, l.tz, m.config AS campaign_cfg
               FROM calls k LEFT JOIN contacts c ON c.id = k.contact_id
               LEFT JOIN campaign_leads l ON l.id = k.lead_id LEFT JOIN campaigns m ON m.id = k.campaign_id
               WHERE k.id = ?""", (call_id,)).fetchone())
    items = db.jload(call["transcript"], [])
    if not items and call["recording"]:
        src = call["recording"]
        if not src.startswith("http") and not os.path.exists(src):
            raise RuntimeError(f"recording file missing: {src}")
        items = await assemblyai.transcribe_file(src)
        with db.tx() as con:
            con.execute("UPDATE calls SET transcript = ? WHERE id = ?", (json.dumps(items, ensure_ascii=False), call_id))
    if not items:
        raise RuntimeError("no transcript")
    goal = db.jload(call["campaign_cfg"]).get("goal", "") if call["campaign_cfg"] else ""
    prompt = (f"Contact: {call['contact_name'] or call['number']} {('(' + call['company'] + ')') if call['company'] else ''}\n"
              f"Direction: {'incoming' if call['direction'] == 'in' else 'outgoing'}\n"
              + (f"Campaign goal: {goal}\n" if goal else "")
              + f"\nTranscript:\n{transcript_text(items)}")
    data = await llm.complete_json(s["analysis_llm"], s["analysis_model"], SYSTEM, prompt)
    outcome = data.get("outcome") if data.get("outcome") in outcomes.OUTCOME_SET else ""
    score = data.get("score")
    score = max(0, min(100, int(score))) if isinstance(score, (int, float)) else None
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    cb = outcomes.parse_when(data["callback"], call["tz"] or "") if data.get("callback") else None
    with db.tx() as con:
        cur = con.execute("SELECT disposition, ai_fields, callback_at FROM calls WHERE id = ?", (call_id,)).fetchone()
        merged = {**fields, **db.jload(cur["ai_fields"])}        # what the live AI saved wins
        con.execute(
            """UPDATE calls SET summary=?, sentiment=?, score=?, next_step=?, ai_fields=?, analysis='done',
                      disposition=CASE WHEN disposition='' THEN ? ELSE disposition END,
                      callback_at=COALESCE(callback_at, ?) WHERE id=?""",
            (str(data.get("summary", ""))[:4000], str(data.get("sentiment", ""))[:20], score,
             str(data.get("next_step", ""))[:500], json.dumps(merged, ensure_ascii=False), outcome, cb, call_id))
        if call["contact_id"] and score is not None:
            con.execute("UPDATE contacts SET score = ? WHERE id = ?", (score, call["contact_id"]))
        if call["contact_id"] and outcome in ("interested", "sale"):
            con.execute("UPDATE contacts SET status = ? WHERE id = ? AND status IN ('new','contacted','interested')",
                        ("customer" if outcome == "sale" else "interested", call["contact_id"]))
        if not cur["disposition"] and outcome:
            outcomes.apply_disposition(con, call_id, outcome)
    if fields:
        outcomes.merge_contact_fields(call_id, fields)


TIPS_SYSTEM = """You coach a call-centre agent who is on a live phone call right now.
Give ONE short, practical suggestion (max 25 words) for what to say or ask next,
based on the conversation so far. If an objection was raised, suggest how to handle it.
No preamble."""


async def live_tip(transcript, goal=""):
    with db.tx() as con:
        s = db.get_settings(con)
    prompt = (f"Call goal: {goal}\n\n" if goal else "") + "Conversation so far:\n" + transcript[-6000:]
    return (await llm.complete(s["analysis_llm"], s["analysis_model"], TIPS_SYSTEM, prompt, effort="low",
                               max_tokens=1024)).strip()
