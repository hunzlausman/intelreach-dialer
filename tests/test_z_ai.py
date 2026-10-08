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
                                         "config": {**ALWAYS, "ai_agent_id": agent["id"], "concurrency": 2}}).json()
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
