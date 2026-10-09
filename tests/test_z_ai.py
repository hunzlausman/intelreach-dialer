"""Campaigns, AI agents, provider webhooks and the voice engine – with every
external provider (Telnyx, ElevenLabs, AssemblyAI, Deepgram, LLMs) mocked."""
import asyncio
import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx2 as httpx  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db, dialer, net, security, vault  # noqa: E402
from app.ai import llm  # noqa: E402
from app.main import app  # noqa: E402
from app.voice import engine  # noqa: E402

TX_KEY = Ed25519PrivateKey.generate()
TX_PUB = base64.b64encode(TX_KEY.public_key().public_bytes(serialization.Encoding.Raw,
                                                           serialization.PublicFormat.Raw)).decode()
SENT = []          # every outgoing provider request: (method, url, json body)


def provider_mock(request: httpx.Request):
    body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith(
        "application/json") else request.content
    SENT.append((request.method, str(request.url), body))
    url = str(request.url)
    if url == "https://api.telnyx.com/v2/calls":
        return httpx.Response(200, json={"data": {"call_control_id": f"cc-{len(SENT)}"}})
    if "/actions/" in url or url.endswith("/balance"):
        return httpx.Response(200, json={"data": {"result": "ok", "balance": "12.5", "currency": "USD"}})
    if "convai/sip-trunk/outbound-call" in url:
        return httpx.Response(200, json={"success": True, "conversation_id": "conv-1", "sip_call_id": "x"})
    if url == "https://api.deepgram.com/v1/projects":
        return httpx.Response(200, json={"projects": [{"project_id": "p1", "name": "IntelReach"}]})
    if url == "https://api.deepgram.com/v1/auth/grant":
        return httpx.Response(200, json={"access_token": "dg-jwt", "expires_in": body["ttl_seconds"]})
    if url.startswith("https://api.deepgram.com/v1/listen?"):
        return httpx.Response(200, json={"results": {"utterances": [
            {"speaker": 0, "transcript": "Hello, this is Sam."}, {"speaker": 1, "transcript": "Hi Sam, go ahead."}]}})
    if url.startswith("http://llm.test/v1/chat/completions"):
        reply = {"summary": "Wants a demo next week.", "outcome": "interested", "sentiment": "positive", "score": 82,
                 "next_step": "Send demo invite", "callback": "", "fields": {"budget": "5k"}}
        chunks = [json.dumps({"choices": [{"delta": {"content": json.dumps(reply)[:20]}}]}),
                  json.dumps({"choices": [{"delta": {"content": json.dumps(reply)[20:]}}]})]
        sse = "".join(f"data: {c}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
    return httpx.Response(404, json={"message": "not mocked: " + url})


def telnyx_event(client, event_type, payload):
    body = json.dumps({"data": {"event_type": event_type, "payload": payload}}).encode()
    ts = str(int(time.time()))
    sig = base64.b64encode(TX_KEY.sign(ts.encode() + b"|" + body)).decode()
    return client.post("/webhooks/telnyx", content=body, headers={
        "telnyx-signature-ed25519": sig, "telnyx-timestamp": ts, "content-type": "application/json"})


def state(call_id, **kw):
    return base64.b64encode(json.dumps({"call_id": call_id, **kw}).encode()).decode()


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def c():
    net.transport = httpx.MockTransport(provider_mock)
    with TestClient(app) as client:
        with db.tx() as con:
            if not con.execute("SELECT 1 FROM users WHERE email = 'a@x.com'").fetchone():
                con.execute("INSERT INTO users(email, name, role, pw_hash, sip_ext) VALUES ('a@x.com','Admin','admin',?,"
                            "'2001')", (security.hash_password("password1"),))
            db.set_setting(con, "twilio_number", "+15550001111")
            db.set_setting(con, "allowed_prefixes", "*")
        assert client.post("/api/login", json={"email": "a@x.com", "password": "password1"}).status_code == 200
        for prov, vals in {"telnyx": {"api_key": "KEYtx", "connection_id": "conn1", "public_key": TX_PUB,
                                      "from_number": "+15550002222"},
                           "elevenlabs": {"api_key": "el-key", "webhook_secret": "whsec"},
                           "assemblyai": {"api_key": "aai-key"},
                           "custom_llm": {"base_url": "http://llm.test/v1", "api_key": "k"}}.items():
            assert client.put(f"/api/admin/integrations/{prov}", json=vals).status_code == 200
        client.put("/api/admin/settings", json={"analysis_llm": "custom_llm", "analysis_model": "m1"})
        yield client
    net.transport = None


def contacts(c, *people):
    ids = []
    for name, number, tags in people:
        r = c.post("/api/contacts", json={"name": name, "phone": number, "tags": tags})
        ids.append(r.json()["id"] if r.status_code == 200 else
                   c.get("/api/contacts", params={"q": number}).json()["items"][0]["id"])
    return ids


ALWAYS = {"days": "1,2,3,4,5,6,7", "window_start": "00:00", "window_end": "23:59", "max_attempts": 2,
          "retry_minutes": 30}


def test_integrations_are_encrypted_and_masked(c):
    items = {i["provider"]: i for i in c.get("/api/admin/integrations").json()["items"]}
    assert items["telnyx"]["api_key"].startswith("••••") and "KEYtx" not in json.dumps(items)
    assert items["telnyx"]["connection_id"] == "conn1"
    with db.tx() as con:
        raw = con.execute("SELECT secret FROM integrations WHERE provider='telnyx'").fetchone()["secret"]
    assert "KEYtx" not in raw
    # empty secret keeps the stored one
    c.put("/api/admin/integrations/telnyx", json={"api_key": "", "connection_id": "conn1"})
    assert vault.key("telnyx") == "KEYtx"
    r = c.post("/api/admin/integrations/telnyx/test").json()
    assert r["ok"] and "12.5" in r["message"]


def test_power_dialer_campaign(c):
    ids = contacts(c, ("Pia One", "+12025550101", "pd"), ("Pia Two", "+12025550102", "pd"))
    camp = c.post("/api/campaigns", json={"name": "PD", "kind": "power",
                                         "config": {**ALWAYS, "script": "Hi {{first_name}}, this is {{agent}}",
                                                    "trunk": "telnyx"}}).json()
    assert c.post(f"/api/campaigns/{camp['id']}/status", json={"status": "running"}).status_code == 400   # no leads
    assert c.post(f"/api/campaigns/{camp['id']}/leads", json={"all_matching": True, "tag": "pd"}).json()["added"] == 2
    assert c.post(f"/api/campaigns/{camp['id']}/status", json={"status": "running"}).json()["status"] == "running"
    nxt = c.post(f"/api/campaigns/{camp['id']}/next").json()
    assert nxt["lead"] and nxt["script"].startswith("Hi Pia, this is Admin")
    lead, contact = nxt["lead"], nxt["contact"]
    call = c.post("/api/calls", json={"number": contact["phone"], "contact_id": contact["id"],
                                      "lead_id": lead["id"]}).json()
    # agent call through the Telnyx trunk (old campaign value "telnyx"), still showing the Twilio number
    r = c.get("/ast/authorize", params={"s": "sek", "ext": "2001", "to": contact["phone"], "call": call["id"]}).text
    assert r.split("|")[1] == "+15550001111" and r.split("|")[5] == "crm-trunk-2"
    c.get("/ast/hangup", params={"s": "sek", "call": call["id"], "status": "NOANSWER"})
    leads = {x["id"]: x for x in c.get(f"/api/campaigns/{camp['id']}/leads").json()["items"]}
    assert leads[lead["id"]]["status"] == "retry" and leads[lead["id"]]["attempts"] == 1
    # the next lead is the other contact (retry is 30 min away)
    nxt2 = c.post(f"/api/campaigns/{camp['id']}/next").json()
    assert nxt2["lead"]["id"] != lead["id"]
    call2 = c.post("/api/calls", json={"number": nxt2["contact"]["phone"], "lead_id": nxt2["lead"]["id"]}).json()
    c.get("/ast/hangup", params={"s": "sek", "call": call2["id"], "status": "ANSWER", "answered": "40"})
    c.patch(f"/api/calls/{call2['id']}", json={"disposition": "do-not-call", "notes": "asked to stop"})
    assert c.get(f"/api/contacts/{nxt2['contact']['id']}").json()["dnc"] == 1
    assert c.post("/api/calls", json={"number": nxt2["contact"]["phone"]}).status_code == 403
    assert c.post(f"/api/campaigns/{camp['id']}/next").json()["lead"] is None


def test_ai_campaign_dialer_and_telnyx_events(c):
    agent = c.post("/api/ai-agents", json={"name": "Ava", "kind": "custom", "config": {
        "prompt": "You qualify leads for {{company}}.", "first_message": "Hi {{first_name}}!", "carrier": "telnyx",
        "llm_provider": "custom_llm", "llm_model": "m1", "voice_id": "v1", "fields": "budget: monthly budget"}}).json()
    contacts(c, ("Ai One", "+12025550201", "ai"), ("Ai Two", "+12025550202", "ai"), ("Ai Three", "+12025550203", "ai"))
    camp = c.post("/api/campaigns", json={"name": "AI", "kind": "ai",
                                         "config": {**ALWAYS, "ai_agent_id": agent["id"], "concurrency": 2,
                                                    "ai_carrier": "agent"}}).json()
    c.post(f"/api/campaigns/{camp['id']}/leads", json={"all_matching": True, "tag": "ai"})
    c.post(f"/api/campaigns/{camp['id']}/status", json={"status": "running"})
    SENT.clear()
    run(dialer.tick())
    dials = [b for m, u, b in SENT if u == "https://api.telnyx.com/v2/calls"]
    assert len(dials) == 2                                     # concurrency 2
    assert dials[0]["stream_bidirectional_mode"] == "rtp" and "/media/telnyx/" in dials[0]["stream_url"]
    assert dials[0]["from"] == "+15550002222" and dials[0]["connection_id"] == "conn1"
    run(dialer.tick())
    assert len([1 for m, u, b in SENT if u == "https://api.telnyx.com/v2/calls"]) == 2   # slots still full
    calls = c.get("/api/calls", params={"campaign": camp["id"]}).json()["items"]
    first = calls[-1]
    with db.tx() as con:
        ext = con.execute("SELECT external_id FROM calls WHERE id = ?", (first["id"],)).fetchone()["external_id"]
    assert telnyx_event(c, "call.answered", {"call_control_id": ext, "client_state": state(first["id"], mode="custom")}).status_code == 200
    telnyx_event(c, "call.hangup", {"call_control_id": ext, "client_state": state(first["id"], mode="custom"),
                                    "hangup_cause": "normal_clearing", "start_time": "2026-10-09T10:00:00Z",
                                    "end_time": "2026-10-09T10:01:05Z"})
    k = c.get(f"/api/calls/{first['id']}").json()
    assert k["status"] == "answered" and k["duration"] == 65
    second = calls[-2]
    with db.tx() as con:
        ext2 = con.execute("SELECT external_id FROM calls WHERE id = ?", (second["id"],)).fetchone()["external_id"]
    telnyx_event(c, "call.hangup", {"call_control_id": ext2, "client_state": state(second["id"]), "hangup_cause": "timeout"})
    leads = {x["contact_id"]: x for x in c.get(f"/api/campaigns/{camp['id']}/leads").json()["items"]}
    assert sorted(x["status"] for x in leads.values()) == ["calling", "done", "retry"] or \
        sorted(x["status"] for x in leads.values()) == ["done", "pending", "retry"]
    # forged events are refused
    r = c.post("/webhooks/telnyx", json={"data": {"event_type": "call.hangup", "payload": {}}})
    assert r.status_code == 403
    c.post(f"/api/campaigns/{camp['id']}/status", json={"status": "paused"})


def test_voicemail_drop(c):
    ids = contacts(c, ("Vm One", "+12025550301", "vm"))
    camp = c.post("/api/campaigns", json={"name": "VM", "kind": "voicemail", "config": {
        **ALWAYS, "vm_text": "Hi, call us back!", "vm_tts": "telnyx", "on_human": "hangup"}}).json()
    c.post(f"/api/campaigns/{camp['id']}/leads", json={"contact_ids": ids})
    c.post(f"/api/campaigns/{camp['id']}/status", json={"status": "running"})
    SENT.clear()
    run(dialer.tick())
    dial = [b for m, u, b in SENT if u == "https://api.telnyx.com/v2/calls"][0]
    assert dial["answering_machine_detection"] == "greeting_end"
    call = c.get("/api/calls", params={"campaign": camp["id"]}).json()["items"][0]
    with db.tx() as con:
        ext = con.execute("SELECT external_id FROM calls WHERE id = ?", (call["id"],)).fetchone()["external_id"]
    st = state(call["id"], mode="voicemail", campaign_id=camp["id"])
    telnyx_event(c, "call.answered", {"call_control_id": ext, "client_state": st})
    telnyx_event(c, "call.machine.greeting.ended", {"call_control_id": ext, "client_state": st, "result": "beep_detected"})
    speak = [b for m, u, b in SENT if u.endswith(f"/calls/{ext}/actions/speak")]
    assert speak and speak[0]["payload"] == "Hi, call us back!"
    telnyx_event(c, "call.speak.ended", {"call_control_id": ext, "client_state": st})
    assert any(u.endswith(f"/calls/{ext}/actions/hangup") for m, u, b in SENT)
    telnyx_event(c, "call.hangup", {"call_control_id": ext, "client_state": st, "hangup_cause": "normal_clearing"})
    assert c.get(f"/api/calls/{call['id']}").json()["status"] == "voicemail-dropped"
    lead = c.get(f"/api/campaigns/{camp['id']}/leads").json()["items"][0]
    assert lead["status"] == "done"
    run(dialer.tick())
    assert c.get(f"/api/campaigns/{camp['id']}").json()["status"] == "completed"


def test_elevenlabs_agent_and_analysis(c):
    agent = c.post("/api/ai-agents", json={"name": "EL", "kind": "elevenlabs", "config": {
        "el_agent_id": "agent_1", "el_phone_number_id": "phnum_1", "el_phone_type": "sip_trunk"}}).json()
    r = c.post(f"/api/ai-agents/{agent['id']}/test-call", json={"number": "+12025550401"})
    assert r.status_code == 200
    call_id = r.json()["call_id"]
    req = [b for m, u, b in SENT if "outbound-call" in u][-1]
    assert req["agent_id"] == "agent_1" and req["to_number"] == "+12025550401"
    payload = {"type": "post_call_transcription", "data": {
        "conversation_id": "conv-1", "status": "done",
        "transcript": [{"role": "agent", "message": "Hi!"}, {"role": "user", "message": "I'd like a demo."}],
        "analysis": {"transcript_summary": "Demo wanted", "data_collection_results": {"budget": {"value": "5k"}}},
        "metadata": {"call_duration_secs": 42}}}
    body = json.dumps(payload).encode()
    t = str(int(time.time()))
    sig = hmac.new(b"whsec", f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    assert c.post("/webhooks/elevenlabs", content=body, headers={"ElevenLabs-Signature": "t=0,v0=x"}).status_code == 403
    assert c.post("/webhooks/elevenlabs", content=body, headers={"ElevenLabs-Signature": f"t={t},v0={sig}"}).status_code == 200
    for _ in range(50):                                  # analysis runs in the background
        k = c.get(f"/api/calls/{call_id}").json()
        if k["analysis"] in ("done",) or k["analysis"].startswith("error"):
            break
        time.sleep(0.1)
    assert k["status"] == "answered" and k["duration"] == 42 and k["transcript"][1]["role"] == "contact"
    assert k["analysis"] == "done", k["analysis"]
    assert k["summary"] == "Wants a demo next week." and k["score"] == 82 and k["disposition"] == "interested"
    assert k["ai_fields"]["budget"] == "5k"


def test_inbound_ai_after_agents(c):
    agent = c.get("/api/ai-agents").json()["items"]
    custom = next(a for a in agent if a["kind"] == "custom")
    c.put("/api/admin/settings", json={"inbound_mode": "crm_then_ai", "inbound_ai_agent": str(custom["id"])})
    from test_flow import twilio
    r = twilio(c, "/twilio/voice", {"CallSid": "CA9", "From": "+12025550999", "To": "+15550001111"})
    call_id = r.text.split("X-CRM-Call=")[1].split("<")[0]
    r = twilio(c, f"/twilio/after-dial?call={call_id}", {"CallSid": "CA9", "DialCallStatus": "no-answer",
                                                         "From": "+12025550999", "To": "+15550001111"})
    assert "<Connect><Stream url=\"wss://crm.example.com/media/twilio/" in r.text
    c.put("/api/admin/settings", json={"inbound_mode": "crm_then_ghl"})


def test_anthropic_history_conversion():
    hist = [{"role": "user", "text": "hi"},
            {"role": "assistant", "text": "", "tool_calls": [{"id": "t1", "name": "a", "input": {}},
                                                              {"id": "t2", "name": "b", "input": {}}]},
            {"role": "tool", "id": "t1", "name": "a", "result": "ok"},
            {"role": "tool", "id": "t2", "name": "b", "result": "ok"}]
    msgs = llm.to_anthropic(hist)
    assert len(msgs) == 3 and [b["tool_use_id"] for b in msgs[2]["content"]] == ["t1", "t2"]
    oa = llm.to_openai("sys", hist)
    assert oa[0]["role"] == "system" and oa[2]["tool_calls"][1]["function"]["name"] == "b" and oa[3]["role"] == "tool"


def test_voice_engine_over_twilio_stream(c, monkeypatch):
    """Full custom-pipeline call over the media WebSocket with fake STT/LLM/TTS."""
    from app.ai import assemblyai, elevenlabs
    from app.voice import carriers

    class FakeSTT:
        def __init__(self, on_partial, on_turn, **kw):
            self.on_turn, self.n = on_turn, 0

        async def start(self):
            pass

        async def send(self, audio):
            self.n += 1
            if self.n == 3:
                await self.on_turn("Yes, our budget is five thousand. Call me back Monday at 10.")

        async def close(self):
            pass

    replies = iter([
        [("text", "Great, thanks! "), ("done", {"text": "Great, thanks!", "raw": None, "tool_calls": [
            {"id": "1", "name": "save_lead_info", "input": {"fields": {"budget": "5000"}}},
            {"id": "2", "name": "schedule_callback", "input": {"when": "2026-10-12T10:00", "note": "Monday call"}}]})],
        [("text", "Talk Monday. Bye!"), ("done", {"text": "Talk Monday. Bye!", "raw": None, "tool_calls": [
            {"id": "3", "name": "end_call", "input": {"reason": "done"}}]})],
    ])

    async def fake_llm(*a, **kw):
        for ev in next(replies):
            yield ev

    async def fake_tts(text, voice, model="", fmt=""):
        yield b"\x7f" * 400

    hung = []

    async def fake_hangup(provider, ext):
        hung.append((provider, ext))

    monkeypatch.setattr(assemblyai, "StreamingSTT", FakeSTT)
    monkeypatch.setattr(llm, "stream_chat", fake_llm)
    monkeypatch.setattr(elevenlabs, "tts_stream", fake_tts)
    monkeypatch.setattr(carriers, "hangup", fake_hangup)

    agent = next(a for a in c.get("/api/ai-agents").json()["items"] if a["kind"] == "custom")
    cid = contacts(c, ("Eve Engine", "+12025550501", ""))[0]
    with db.tx() as con:
        call_id = con.execute("INSERT INTO calls(direction, number, contact_id, ai_agent_id, provider, status) "
                              "VALUES ('out', '+12025550501', ?, ?, 'twilio', 'dialing')", (cid, agent["id"])).lastrowid
    token = engine.new_token({"call_id": call_id, "agent_id": agent["id"],
                              "vars": engine.contact_vars({"name": "Eve Engine", "company": "Acme"})})
    from app.voice import media as media_mod
    frames = []
    orig_send = media_mod.Transport.send_audio

    async def spy(self, frame):
        frames.append(self.stream_sid)
        await orig_send(self, frame)

    monkeypatch.setattr(media_mod.Transport, "send_audio", spy)
    with c.websocket_connect(f"/media/twilio/{token}") as ws:
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ1", "callSid": "CAeng"}}))
        for _ in range(4):
            ws.send_text(json.dumps({"event": "media", "media": {"payload": base64.b64encode(bytes([255]) * 160).decode()}}))
        deadline = time.time() + 10
        while time.time() < deadline and not hung:
            time.sleep(0.1)
        ws.send_text(json.dumps({"event": "stop"}))
    assert hung == [("twilio", "CAeng")]
    assert frames and set(frames) == {"MZ1"}
    for _ in range(30):
        k = c.get(f"/api/calls/{call_id}").json()
        if k["ended_at"]:
            break
        time.sleep(0.1)
    roles = [t["role"] for t in k["transcript"]]
    assert roles[:3] == ["agent", "contact", "agent"] and k["transcript"][0]["text"] == "Hi Eve!"
    assert k["ai_fields"]["budget"] == "5000" and k["disposition"] == "callback" and k["callback_at"]
    assert c.get(f"/api/contacts/{cid}").json()["custom"].count("5000")
    # a stream with an unknown token is refused
    with pytest.raises(Exception):
        with c.websocket_connect("/media/twilio/nope") as ws:
            ws.receive_text()


def test_claude_streaming_with_tools(c):
    """The Anthropic SDK path: streamed text + a tool call, refusal fallbacks requested."""
    seen = {}

    def claude(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        events = [
            ("message_start", {"type": "message_start", "message": {
                "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
                "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 1}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": "Goodbye now."}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {
                "type": "tool_use", "id": "tu_1", "name": "end_call", "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {
                "type": "input_json_delta", "partial_json": "{\"reason\": \"done\"}"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 1}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                               "usage": {"output_tokens": 12}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        sse = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    c.put("/api/admin/integrations/anthropic", json={"api_key": "sk-ant-test"})
    prev = net.transport
    net.transport = httpx.MockTransport(claude)
    try:
        async def go():
            out = []
            async for ev in llm.stream_chat("anthropic", "claude-opus-5-5", "sys", [{"role": "user", "text": "bye"}],
                                            engine.TOOLS):
                out.append(ev)
            return out
        events = run(go())
    finally:
        net.transport = prev
    assert ("text", "Goodbye now.") in events
    done = events[-1][1]
    assert done["tool_calls"] == [{"id": "tu_1", "name": "end_call", "input": {"reason": "done"}}]
    assert done["raw"][1]["type"] == "tool_use"
    assert seen["body"]["fallbacks"] == "default" and seen["body"]["output_config"] == {"effort": "low"}
    assert "server-side-fallback-2026-07-01" in seen["beta"]
    assert seen["body"]["tools"][0]["eager_input_streaming"] is True


def test_deepgram_speech_to_text(c, tmp_path):
    from app.ai import assemblyai, deepgram, stt
    assert c.put("/api/admin/integrations/deepgram", json={"api_key": "dg-key-0123456789", "language": "en"}).status_code == 200
    items = {i["provider"]: i for i in c.get("/api/admin/integrations").json()["items"]}
    assert items["deepgram"]["configured"] and "dg-key-0123456789" not in json.dumps(items)
    r = c.post("/api/admin/integrations/deepgram/test").json()
    assert r["ok"] and "IntelReach" in r["message"]
    assert c.put("/api/admin/settings", json={"stt_provider": "whisper"}).status_code == 400
    c.put("/api/admin/settings", json={"stt_provider": "deepgram", "live_captions": "1"})

    # live captions: the browser gets a short-lived Deepgram token + stream settings, never the key
    sess = c.get("/api/ai/stt-token").json()
    assert sess["provider"] == "deepgram" and sess["token"] == "dg-jwt"
    assert sess["params"]["encoding"] == "linear16" and sess["params"]["sample_rate"] == 16000
    assert sess["params"]["language"] == "en" and sess["params"]["model"] == "nova-3"
    assert SENT[-1][1].endswith("/auth/grant") and SENT[-1][2] == {"ttl_seconds": 300}

    # recording transcript with speakers
    rec = tmp_path / "1.wav"
    rec.write_bytes(b"RIFF....WAVE")
    out = run(stt.transcribe_file(str(rec)))
    assert out == [{"role": "speaker 0", "text": "Hello, this is Sam."}, {"role": "speaker 1", "text": "Hi Sam, go ahead."}]
    method, url, body = SENT[-1]
    assert "diarize=true" in url and "language=en" in url and body == b"RIFF....WAVE"

    # AI agents: settings choose Deepgram, an agent can still pick AssemblyAI
    noop = lambda *_: None  # noqa: E731
    assert isinstance(stt.streaming(noop, noop, language="es"), deepgram.StreamingSTT)
    assert "language=es" in stt.streaming(noop, noop, language="es").url
    assert isinstance(stt.streaming(noop, noop, override="assemblyai"), assemblyai.StreamingSTT)
    assert c.post("/api/ai-agents", json={"name": "x", "kind": "custom",
                                          "config": {"prompt": "hi", "stt_provider": "nope"}}).status_code == 400

    # interim / final pieces become partials and whole turns
    got = []

    async def part(t):
        got.append(("partial", t))

    async def turn(t):
        got.append(("turn", t))

    turns = deepgram.Turns(part, turn)
    for msg in ({"type": "Results", "is_final": False, "channel": {"alternatives": [{"transcript": "I want"}]}},
                {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": "I want a demo"}]}},
                {"type": "Results", "is_final": False, "channel": {"alternatives": [{"transcript": "next"}]}},
                {"type": "Results", "is_final": True, "speech_final": True,
                 "channel": {"alternatives": [{"transcript": "next week."}]}},
                {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": "Thanks"}]}},
                {"type": "UtteranceEnd"}):
        run(turns.handle(msg))
    assert got == [("partial", "I want"), ("partial", "I want a demo"), ("partial", "I want a demo next"),
                   ("turn", "I want a demo next week."), ("partial", "Thanks"), ("turn", "Thanks")]
    c.put("/api/admin/settings", json={"stt_provider": "assemblyai", "live_captions": "0"})



def test_ulaw_codec_round_trip():
    from app.voice import audiosocket as a
    every = bytes(i for i in range(256) if i != 0x7F)          # 0x7F is μ-law "-0" and comes back as 0xFF
    assert a.pcm_to_ulaw(a.ulaw_to_pcm(every)) == every
    assert a.ulaw_to_pcm(b"\xff") == b"\x00\x00" and a.pcm_to_ulaw(b"\x00\x00") == b"\xff"
    assert a.pcm_to_ulaw((32767).to_bytes(2, "little", signed=True)) == b"\x80"
    # caller audio from Asterisk 18 comes in the line's codec: μ-law passes through, a-law is converted
    assert a.codec_of("(ulaw)") == "ulaw" and a.codec_of("(alaw|ulaw)") == "alaw" and a.codec_of("(slin)") == ""
    voice = bytes(range(0, 256, 2)) + bytes(32)
    assert a.to_ulaw(voice[:160], "ulaw") == voice[:160]
    assert max(127 - (b & 0x7F) for b in a.to_ulaw(b"\xd5" * 160, "alaw")) <= 1   # a-law silence -> μ-law ~silence
    assert a.pcm_to_ulaw(a.ulaw_to_pcm(a.to_ulaw(bytes([0x2A]), "alaw"))) == a.to_ulaw(bytes([0x2A]), "alaw")
    assert a.to_ulaw(b"\x00" * 320, "") == b"\xff" * 160                       # 16-bit frame, no hint
    assert a.to_ulaw(b"\x7f" * 160, "") == b"\x7f" * 160                       # 8-bit frame, no hint


def test_ai_agent_over_sip_trunk(c, monkeypatch):
    """Custom AI agent on the SIP trunk: call file -> Asterisk (simulated) -> AudioSocket -> voice engine."""
    import socket
    import uuid as uuidlib
    from app import config
    from app.ai import assemblyai, elevenlabs
    from app.voice import audiosocket

    class FakeSTT:
        def __init__(self, on_partial, on_turn, **kw):
            self.on_turn, self.n = on_turn, 0

        async def start(self):
            pass

        async def send(self, audio):
            assert len(audio) == 160                           # 320 bytes of 16-bit audio -> 160 μ-law bytes
            self.n += 1
            if self.n == 3:
                await self.on_turn("I'd like to talk to a person please.")

        async def close(self):
            pass

    async def fake_llm(*a, **kw):
        for ev in [("text", "Sure, connecting you now."), ("done", {"text": "Sure, connecting you now.", "raw": None,
                   "tool_calls": [{"id": "1", "name": "transfer_to_human", "input": {"reason": "asked"}}]})]:
            yield ev

    async def fake_tts(text, voice, model="", fmt=""):
        yield b"\xff" * 480

    monkeypatch.setattr(assemblyai, "StreamingSTT", FakeSTT)
    monkeypatch.setattr(llm, "stream_chat", fake_llm)
    monkeypatch.setattr(elevenlabs, "tts_stream", fake_tts)

    base = {"prompt": "Book demos.", "llm_provider": "custom_llm", "llm_model": "m1", "tts_provider": "elevenlabs",
            "voice_id": "v1", "first_message": "Hi {{first_name}}!", "transfer_to": "sip:agents"}
    assert c.post("/api/ai-agents", json={"name": "bad", "kind": "custom",
                                          "config": {**base, "carrier": "sip", "tts_provider": "telnyx"}}).status_code == 400
    aid = c.post("/api/ai-agents", json={"name": "SIP Sara", "kind": "custom", "config": {**base, "carrier": "sip"}}).json()["id"]
    contacts(c, ("Sam Sip", "+12025550601", ""))

    # outbound: the CRM writes an Asterisk call file for the default trunk with the trunk's caller ID
    call_id = c.post(f"/api/ai-agents/{aid}/test-call", json={"number": "+12025550601"}).json()["call_id"]
    cf = Path(config.AST_SPOOL) / f"crm-ai-{call_id}.call"
    text = cf.read_text()
    assert "Channel: PJSIP/+12025550601@crm-trunk-1\n" in text and "CallerID: <+15550001111>" in text
    assert "Context: crm-ai\n" in text and f"Setvar: CRMID={call_id}\n" in text
    uid = text.split("Setvar: AIUUID=")[1].split("\n")[0]
    cf.unlink()

    # Asterisk: answered -> AudioSocket with the uuid; the caller asks for a person -> transfer to the CRM agents
    c.post("/api/me/heartbeat", json={"available": True})
    assert c.get("/ast/ai-answer", params={"s": "sek", "call": call_id, "fmt": "(slin)"}).text == "ok|0"
    assert c.get("/ast/ai-answer", params={"s": "sek", "call": call_id}).text == "none|"     # a duplicate leg is refused
    assert [p.name for p in Path(config.AST_SPOOL).iterdir()] == [] and not list(Path(config.AST_SPOOL_TMP).iterdir())
    got, kinds = b"", []
    with socket.create_connection(("127.0.0.1", audiosocket.PORT), timeout=10) as sock:
        sock.sendall(b"\x01\x00\x10" + uuidlib.UUID(uid).bytes)
        for _ in range(4):
            sock.sendall(b"\x10\x01\x40" + b"\x00" * 320)
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            got += chunk
    while got:                                               # parse what the CRM sent back
        kind, n = got[0], int.from_bytes(got[1:3], "big")
        kinds.append((kind, n))
        got = got[3 + n:]
    assert kinds[0] == (0x10, 320) and kinds[-1] == (0x00, 0)    # audio frames (16-bit, 20 ms), then hang-up
    assert c.get("/ast/ai-next", params={"s": "sek", "call": call_id}).text == "agents|PJSIP/2001|20"
    assert c.get("/ast/ai-next", params={"s": "sek", "call": call_id}).text == "hangup|"
    deadline = time.time() + 10
    while time.time() < deadline and not c.get(f"/api/calls/{call_id}").json()["ended_at"]:
        time.sleep(0.1)
    k = c.get(f"/api/calls/{call_id}").json()
    assert k["status"] == "answered" and k["provider"] == "sip"
    roles = [t["role"] for t in k["transcript"]]
    assert roles[:2] == ["agent", "contact"] and k["transcript"][0]["text"] == "Hi Sam!" and "system" in roles
    assert c.get("/ast/ai-hangup", params={"s": "sek", "call": call_id, "answered": "9"}).text == "ok"

    # a forged / unknown uuid never starts a session
    with socket.create_connection(("127.0.0.1", audiosocket.PORT), timeout=5) as sock:
        sock.sendall(b"\x01\x00\x10" + uuidlib.uuid4().bytes)
        assert sock.recv(10) == b""

    # not answered: the call file's "failed" extension reports busy
    call2 = c.post(f"/api/ai-agents/{aid}/test-call", json={"number": "+12025550601"}).json()["call_id"]
    assert c.get("/ast/ai-failed", params={"s": "sek", "call": call2, "reason": "5"}).text == "ok"
    assert c.get(f"/api/calls/{call2}").json()["status"] == "busy"
    assert not any(i["call_id"] == call2 for i in engine.PENDING.values())
    (Path(config.AST_SPOOL) / f"crm-ai-{call2}.call").unlink()

    # inbound: a trunk number answered by the AI agent (or after the agents)
    assert c.post("/api/admin/numbers", json={"number": "+15550009999", "trunk_id": 2, "inbound": "ai"}).status_code == 400
    n = c.post("/api/admin/numbers", json={"number": "+15550009999", "trunk_id": 2, "inbound": "ai",
                                           "ai_agent_id": aid}).json()
    r = c.get("/ast/inbound", params={"s": "sek", "did": "+15550009999", "src": "+12025550601"}).text
    assert r.startswith("ai|Sam Sip|0|") and r.endswith("|1")
    cid3 = r.split("|")[4]
    ok, uid3 = c.get("/ast/ai-start", params={"s": "sek", "call": cid3}).text.split("|")
    assert ok == "ok" and engine.PENDING[uid3]["agent_id"] == aid and engine.PENDING[uid3]["call_id"] == int(cid3)
    engine.PENDING.pop(uid3)
    c.put(f"/api/admin/numbers/{n['id']}", json={"number": "+15550009999", "trunk_id": 2, "inbound": "agents_then_ai",
                                                  "ai_agent_id": aid})
    r = c.get("/ast/inbound", params={"s": "sek", "did": "+15550009999", "src": "+12025550601"}).text
    assert r.startswith("PJSIP/2001|Sam Sip|") and r.endswith("|1")
    c.delete(f"/api/admin/numbers/{n['id']}")



def test_ai_campaign_calls_through_the_sip_trunk_by_default(c, monkeypatch):
    """A campaign uses the SIP trunk even if its Custom agent was saved with the Telnyx API carrier."""
    from app import config, launcher
    agent = next(a for a in c.get("/api/ai-agents").json()["items"]
                 if a["kind"] == "custom" and a["config"].get("carrier") == "telnyx")
    contacts(c, ("Tia Trunk", "+12025550701", "trunkcamp"))
    assert c.post("/api/campaigns", json={"name": "x", "kind": "ai", "config": {"ai_carrier": "carrier-pigeon"}}).status_code == 400
    assert c.post("/api/campaigns", json={"name": "x", "kind": "ai", "config": {"from_number": "123"}}).status_code == 400
    camp = c.post("/api/campaigns", json={"name": "Trunk AI", "kind": "ai", "config": {
        **ALWAYS, "ai_agent_id": agent["id"], "from_number": "+15550001111"}}).json()
    c.post(f"/api/campaigns/{camp['id']}/leads", json={"all_matching": True, "tag": "trunkcamp"})
    assert c.post(f"/api/campaigns/{camp['id']}/status", json={"status": "running"}).json()["status"] == "running"
    SENT.clear()
    run(dialer.tick())
    files = list(Path(config.AST_SPOOL).glob("crm-ai-*.call"))
    assert len(files) == 1 and not [u for _, u, _ in SENT if "telnyx" in u]       # no Telnyx API request
    text = files[0].read_text()
    assert "Channel: PJSIP/+12025550701@crm-trunk-1\n" in text and "CallerID: <+15550001111>" in text
    files[0].unlink()
    c.post(f"/api/campaigns/{camp['id']}/status", json={"status": "paused"})
    engine.PENDING.clear()

    # what is missing is reported when the campaign starts, not later
    monkeypatch.setattr(vault, "key", lambda *a, **k: "")
    with db.tx() as con:
        a = launcher.load_agent(con, agent["id"])
        assert "SIP trunk" in launcher.route_problem(con, a, "agent")              # Telnyx carrier, no Telnyx key
        assert "ElevenLabs API key missing" in launcher.route_problem(con, a, "sip")
    r = c.post(f"/api/campaigns/{camp['id']}/status", json={"status": "running"})
    assert r.status_code == 400 and "ElevenLabs" in r.json()["detail"]

    # startup fix: a Custom agent on the Telnyx API carrier without a Telnyx key is moved to the SIP trunk
    launcher.fix_agent_carriers()
    fixed = next(x for x in c.get("/api/ai-agents").json()["items"] if x["id"] == agent["id"])
    assert fixed["config"]["carrier"] == "sip"
    assert c.get("/api/health").json()["version"] and c.get("/app.js").headers["cache-control"] == "no-cache"


def test_stt_uses_the_provider_that_has_a_key(c, monkeypatch):
    from app.ai import stt
    c.put("/api/admin/settings", json={"stt_provider": "assemblyai"})
    keys = {"deepgram": "dg"}
    monkeypatch.setattr(vault, "key", lambda p, field="api_key": keys.get(p, ""))
    assert stt.provider() == "deepgram" and stt.missing() == ""
    keys.clear()
    assert "Speech-to-text API key missing" in stt.missing()



def test_llm_falls_back_to_a_provider_with_a_key(monkeypatch):
    from app.ai import llm as llm_mod
    cfgs = {"gemini": {"api_key": "g"}}
    monkeypatch.setattr(vault, "load", lambda p: cfgs.get(p, {}))
    assert llm_mod.resolve("anthropic", "claude-opus-5-5") == ("gemini", "")
    assert llm_mod.resolve("gemini", "gemini-x") == ("gemini", "gemini-x")
    cfgs.clear()
    assert llm_mod.resolve("anthropic", "m") == ("anthropic", "m")       # nothing set: the original error shows


def test_gemini_list_error_and_retry(c, monkeypatch):
    """Gemini wraps errors in a list; rate limits (429) are retried before the call hears an apology."""
    import httpx2
    from app import net as net_mod
    r = httpx2.Response(429, json=[{"error": {"code": 429, "message": "Resource has been exhausted", "status": "RESOURCE_EXHAUSTED"}}])
    try:
        net_mod.check(r, "gemini")
        assert False
    except net_mod.ProviderError as e:
        assert "Resource has been exhausted" in str(e)
    hits = []

    def flaky(request):
        hits.append(1)
        if len(hits) == 1:
            return httpx2.Response(429, json=[{"error": {"message": "slow down"}}], headers={"retry-after": "0"})
        sse = 'data: {"choices": [{"delta": {"content": "Hi!"}}]}\n\ndata: [DONE]\n\n'
        return httpx2.Response(200, text=sse, headers={"content-type": "text/event-stream"})
    monkeypatch.setattr(net_mod, "transport", httpx2.MockTransport(flaky))
    text = run(llm.complete("custom_llm", "m1", "sys", "hello", max_tokens=10))
    assert text == "Hi!" and len(hits) == 2



def test_assemblyai_llm_gateway(c, monkeypatch):
    """The AssemblyAI key also runs the LLM Gateway (OpenAI-compatible, key without "Bearer")."""
    import httpx2
    from app import net as net_mod
    seen = []

    def gateway(request):
        seen.append((str(request.url), request.headers.get("authorization"), json.loads(request.content)))
        sse = 'data: {"choices": [{"delta": {"content": "OK"}}]}\n\ndata: [DONE]\n\n'
        return httpx2.Response(200, text=sse, headers={"content-type": "text/event-stream"})
    monkeypatch.setattr(net_mod, "transport", httpx2.MockTransport(gateway))
    assert run(llm.complete("assemblyai", "", "sys", "hello", max_tokens=10)) == "OK"
    url, auth, body = seen[0]
    assert url == "https://llm-gateway.assemblyai.com/v1/chat/completions" and auth == "aai-key"
    assert body["model"] == "gemini-2.5-flash" and body["stream"] is True
    agent = c.post("/api/ai-agents", json={"name": "Gw", "kind": "custom", "config": {
        "prompt": "x", "llm_provider": "assemblyai", "llm_model": "claude-haiku-4-5-20251001", "carrier": "sip"}})
    assert agent.status_code == 200



def test_realtime_calls_turn_llm_thinking_off(c, monkeypatch):
    """Live calls send reasoning_effort=none to Gemini Flash; a model that refuses it is retried without."""
    import httpx2
    from app import net as net_mod
    from app.ai import assemblyai
    seen = []

    def api(request):
        body = json.loads(request.content)
        seen.append(body.get("reasoning_effort"))
        if body.get("reasoning_effort") and body["model"] == "picky-model":
            return httpx2.Response(400, json={"error": {"message": "reasoning_effort not supported"}})
        sse = 'data: {"choices": [{"delta": {"content": "Hi"}}]}\n\ndata: [DONE]\n\n'
        return httpx2.Response(200, text=sse, headers={"content-type": "text/event-stream"})
    monkeypatch.setattr(net_mod, "transport", httpx2.MockTransport(api))

    async def collect(provider, model, realtime):
        return [ev async for ev in llm.stream_chat(provider, model, "s", [{"role": "user", "text": "x"}], realtime=realtime)]

    run(collect("assemblyai", "gemini-2.5-flash", True))
    run(collect("assemblyai", "gemini-2.5-flash", False))
    assert seen == ["none", None]
    seen.clear()
    monkeypatch.setattr(llm, "_reasoning_choices", lambda m: ["none"])
    run(collect("assemblyai", "picky-model", True))
    run(collect("assemblyai", "picky-model", True))
    assert seen == ["none", None, None]                  # refused once, then remembered
    assert "min_turn_silence=160" in assemblyai.StreamingSTT(None, None).url



def test_history_shape_for_strict_llms():
    """Greeting first and repeated user turns (after a failed reply) are reshaped for Claude-on-Bedrock style APIs."""
    hist = [{"role": "assistant", "text": "Hi Sam!"}, {"role": "user", "text": "Who is this?"},
            {"role": "user", "text": "Hello?"},
            {"role": "assistant", "text": "", "tool_calls": [{"id": "t1", "name": "save_lead_info", "input": {"x": 1}}]},
            {"role": "tool", "id": "t1", "name": "save_lead_info", "result": {"ok": True}},
            {"role": "assistant", "text": "Sara from Acme."}]
    msgs = llm.to_openai("sys", hist)
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user", "assistant", "tool", "assistant"]
    assert msgs[1]["content"] == llm.CALL_START and msgs[3]["content"] == "Who is this?\nHello?"
    assert msgs[4]["content"] is None and msgs[5]["content"] == '{"ok": true}'
    a = llm.to_anthropic(hist)
    assert a[0] == {"role": "user", "content": llm.CALL_START} and a[2]["content"] == "Who is this?\nHello?"
