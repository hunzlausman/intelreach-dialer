"""AI agents, provider integrations, live captions / tips, transcripts and recordings."""
import json
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from . import config, db, launcher, net, outcomes, phone, vault
from .ai import analysis, assemblyai, deepgram, elevenlabs, llm, stt
from .security import current_user, require_admin
from .voice import carriers

router = APIRouter(prefix="/api")

AGENT_KINDS = {"custom", "elevenlabs", "telnyx"}
AGENT_KEYS = {
    # custom pipeline
    "prompt", "first_message", "llm_provider", "llm_model", "tts_provider", "voice_id", "tts_model", "language",
    "language_name", "carrier", "from_number", "transfer_to", "fields", "max_minutes", "silence_seconds", "record",
    "stt_provider",
    # ElevenLabs agent
    "el_agent_id", "el_phone_number_id", "el_phone_type",
    # Telnyx AI Assistant
    "tx_assistant_id",
}


# ------------------------------------------------------------- AI agents ----

class AgentIn(BaseModel):
    name: str
    kind: str
    config: dict = {}


def clean_agent(kind, cfg):
    if kind not in AGENT_KINDS:
        raise HTTPException(400, "Kind must be custom, elevenlabs or telnyx")
    cfg = {k: v for k, v in (cfg or {}).items() if k in AGENT_KEYS}
    if kind == "custom":
        if cfg.get("llm_provider", "anthropic") not in llm.DEFAULT_MODELS:
            raise HTTPException(400, "Unknown LLM provider")
        if cfg.get("stt_provider") and cfg["stt_provider"] not in stt.PROVIDERS:
            raise HTTPException(400, "Speech-to-text must be assemblyai or deepgram")
        if (cfg.get("carrier") or "sip") not in ("telnyx", "twilio", "sip"):
            raise HTTPException(400, "Carrier must be telnyx, twilio or sip")
        if cfg.get("carrier") == "sip" and cfg.get("tts_provider") == "telnyx":
            raise HTTPException(400, "Telnyx voices only work on Telnyx Call Control calls – choose ElevenLabs")
        if not cfg.get("prompt"):
            raise HTTPException(400, "Write the agent's instructions (prompt)")
        fields = cfg.get("fields") or []
        if isinstance(fields, str):        # "budget: monthly budget\ntimeline: when they want to start"
            fields = [{"name": a.strip(), "description": b.strip()} for a, _, b in
                      (line.partition(":") for line in fields.splitlines()) if a.strip()]
        cfg["fields"] = fields[:30]
    if kind == "elevenlabs" and not (cfg.get("el_agent_id") and cfg.get("el_phone_number_id")):
        raise HTTPException(400, "Choose the ElevenLabs agent and its phone number")
    if kind == "telnyx" and not cfg.get("tx_assistant_id"):
        raise HTTPException(400, "Enter the Telnyx AI Assistant ID")
    if cfg.get("transfer_to") and not (cfg["transfer_to"].startswith("sip:") or phone.E164.match(cfg["transfer_to"])):
        raise HTTPException(400, "Transfer target must be +<number> or a sip: address")
    return cfg


@router.get("/ai-agents")
def list_agents(user=Depends(current_user)):
    with db.tx() as con:
        items = db.rows(con.execute("SELECT * FROM ai_agents ORDER BY name"))
    for a in items:
        a["config"] = db.jload(a["config"])
    return {"items": items}


@router.post("/ai-agents")
def create_agent(body: AgentIn, user=Depends(require_admin)):
    cfg = clean_agent(body.kind, body.config)
    with db.tx() as con:
        aid = con.execute("INSERT INTO ai_agents(name, kind, config) VALUES (?, ?, ?)",
                          (body.name.strip()[:120] or "AI agent", body.kind, json.dumps(cfg))).lastrowid
    return {"id": aid}


@router.put("/ai-agents/{aid}")
def update_agent(aid: int, body: AgentIn, user=Depends(require_admin)):
    cfg = clean_agent(body.kind, body.config)
    with db.tx() as con:
        if not con.execute("UPDATE ai_agents SET name=?, kind=?, config=? WHERE id=?",
                           (body.name.strip()[:120], body.kind, json.dumps(cfg), aid)).rowcount:
            raise HTTPException(404, "AI agent not found")
    return {"ok": True}


@router.delete("/ai-agents/{aid}")
def delete_agent(aid: int, user=Depends(require_admin)):
    with db.tx() as con:
        used = con.execute("SELECT name FROM campaigns WHERE json_extract(config, '$.ai_agent_id') IN (?, ?) "
                           "AND status = 'running'", (aid, str(aid))).fetchone()
        if used:
            raise HTTPException(409, f"Used by running campaign '{used['name']}'")
        con.execute("DELETE FROM ai_agents WHERE id = ?", (aid,))
    return {"ok": True}


class TestCall(BaseModel):
    number: str


@router.post("/ai-agents/{aid}/test-call")
async def test_call(aid: int, body: TestCall, user=Depends(require_admin)):
    with db.tx() as con:
        s = db.get_settings(con)
        num = phone.normalize(body.number, s["default_country"])
        if not num:
            raise HTTPException(400, "Invalid phone number")
        reason = phone.check_allowed(num, s)
        if reason:
            raise HTTPException(403, reason)
        c = db.row(con.execute("SELECT * FROM contacts WHERE phone = ?", (num,)).fetchone())
    try:
        call_id = await launcher.place_ai_call(aid, num, c or {"name": user["name"], "phone": num}, user_id=user["id"])
    except net.ProviderError as e:
        raise HTTPException(502, str(e))
    return {"call_id": call_id}


# ---------------------------------------------------------- integrations ----

@router.get("/admin/integrations")
def list_integrations(user=Depends(require_admin)):
    return {"items": [{"provider": p, "label": spec["label"], "fields": list(spec["fields"]),
                       "secret": [k for k, v in spec["fields"].items() if v], **vault.public(p)}
                      for p, spec in vault.PROVIDERS.items()],
            "webhooks": {"telnyx": f"{config.PUBLIC_URL}/webhooks/telnyx",
                         "elevenlabs": f"{config.PUBLIC_URL}/webhooks/elevenlabs"},
            "defaultModels": llm.DEFAULT_MODELS}


@router.put("/admin/integrations/{provider}")
def save_integration(provider: str, body: dict, user=Depends(require_admin)):
    if provider not in vault.PROVIDERS:
        raise HTTPException(404, "Unknown provider")
    vault.save(provider, body)
    return vault.public(provider)


@router.post("/admin/integrations/{provider}/test")
async def test_integration(provider: str, body: dict = None, user=Depends(require_admin)):
    try:
        if provider == "assemblyai":           # speech-to-text key; the same key runs the LLM Gateway
            await assemblyai.browser_token(60)
            model = (body or {}).get("model") or llm.DEFAULT_MODELS["assemblyai"]
            try:
                text = await llm.complete("assemblyai", model, "Reply with the single word OK.", "Test",
                                          effort="low", max_tokens=256)
                gw = f"LLM Gateway {model}: {text.strip()[:40]}"
            except (net.ProviderError, llm.LLMError) as e:
                gw = f"LLM Gateway not usable: {e}"
            return {"ok": True, "message": f"Speech-to-text OK · {gw}"}
        if provider in llm.DEFAULT_MODELS:
            model = (body or {}).get("model") or llm.DEFAULT_MODELS[provider]
            text = await llm.complete(provider, model, "Reply with the single word OK.", "Test", effort="low", max_tokens=256)
            return {"ok": True, "message": f"{model}: {text.strip()[:80]}"}
        if provider == "assemblyai":
            await assemblyai.browser_token(60)
            return {"ok": True, "message": "Key works (streaming token issued)"}
        if provider == "deepgram":
            projects = await deepgram.check_key()
            await deepgram.browser_token(30)
            return {"ok": True, "message": f"Key works – project {', '.join(projects) or '?'}, caption tokens OK"}
        if provider == "elevenlabs":
            v = await elevenlabs.voices()
            return {"ok": True, "message": f"Key works – {len(v)} voices"}
        if provider == "twilio":
            a = await carriers.twilio_account()
            return {"ok": True, "message": f"Works – account '{a.get('friendly_name', '')}' ({a.get('status', '')})"}
        if provider == "telnyx":
            r = await carriers.telnyx_post_get("/balance")
            bal = (r.get("data") or {})
            return {"ok": True, "message": f"Key works – balance {bal.get('balance')} {bal.get('currency', '')}"}
    except (net.ProviderError, llm.LLMError) as e:
        return {"ok": False, "message": str(e)}
    raise HTTPException(404, "Unknown provider")


@router.get("/admin/elevenlabs/{what}")
async def elevenlabs_lists(what: str, user=Depends(require_admin)):
    fn = {"voices": elevenlabs.voices, "agents": elevenlabs.agents, "phone-numbers": elevenlabs.phone_numbers}.get(what)
    if not fn:
        raise HTTPException(404)
    try:
        return {"items": await fn()}
    except net.ProviderError as e:
        raise HTTPException(502, str(e))


# --------------------------------------------------- AI on human calls ----

@router.get("/ai/stt-token")
async def stt_token(user=Depends(current_user)):
    with db.tx() as con:
        if db.get_settings(con).get("live_captions") != "1":
            raise HTTPException(403, "Live captions are switched off (Admin → Settings)")
    try:
        return await stt.browser_session()
    except net.ProviderError as e:
        raise HTTPException(502, str(e))


class TipIn(BaseModel):
    call_id: int | None = None
    transcript: str


@router.post("/ai/tips")
async def tips(body: TipIn, user=Depends(current_user)):
    goal = ""
    if body.call_id:
        with db.tx() as con:
            r = con.execute("SELECT m.config FROM calls k JOIN campaigns m ON m.id = k.campaign_id WHERE k.id = ?",
                            (body.call_id,)).fetchone()
            goal = db.jload(r["config"]).get("goal", "") if r else ""
    try:
        return {"tip": await analysis.live_tip(body.transcript, goal)}
    except (llm.LLMError, net.ProviderError) as e:
        raise HTTPException(502, str(e))


class TranscriptIn(BaseModel):
    items: list[dict]


@router.post("/calls/{call_id}/transcript")
def save_transcript(call_id: int, body: TranscriptIn, user=Depends(current_user)):
    """Live-caption transcript of a human call (only if no better transcript exists)."""
    items = [{"role": str(i.get("role", ""))[:20], "text": str(i.get("text", ""))[:2000]} for i in body.items][:1000]
    with db.tx() as con:
        k = con.execute("SELECT agent_id, transcript FROM calls WHERE id = ?", (call_id,)).fetchone()
        if not k or k["agent_id"] not in (None, user["id"]):
            raise HTTPException(404, "Call not found")
        if not k["transcript"] and items:
            con.execute("UPDATE calls SET transcript = ? WHERE id = ?", (json.dumps(items, ensure_ascii=False), call_id))
    outcomes.maybe_analyze(call_id)
    return {"ok": True}


@router.post("/calls/{call_id}/analyze")
def reanalyze(call_id: int, user=Depends(current_user)):
    with db.tx() as con:
        con.execute("UPDATE calls SET analysis = '' WHERE id = ?", (call_id,))
    outcomes.maybe_analyze(call_id)
    return {"ok": True}


@router.get("/calls/{call_id}/recording")
def recording(call_id: int, user=Depends(current_user)):
    with db.tx() as con:
        k = con.execute("SELECT recording, agent_id FROM calls WHERE id = ?", (call_id,)).fetchone()
    if not k or not k["recording"] or (user["role"] != "admin" and k["agent_id"] not in (None, user["id"])):
        raise HTTPException(404, "No recording")
    if k["recording"].startswith("http"):
        return RedirectResponse(k["recording"])
    real = os.path.realpath(k["recording"])
    allowed = [os.path.realpath(config.RECORDINGS_DIR), os.path.realpath(config.MEDIA_DIR)]
    if not any(real.startswith(a + os.sep) for a in allowed) or not os.path.exists(real):
        raise HTTPException(404, "Recording file not found")
    return FileResponse(real)
