"""Custom AI voice pipeline:  caller audio -> STT -> LLM (+tools) -> TTS -> caller.

One VoiceSession per call. The carrier (Telnyx or Twilio) streams 8 kHz μ-law audio
over a WebSocket (see voice/media.py); calls on your own SIP trunks come from Asterisk
over AudioSocket (voice/audiosocket.py). The session answers on the same connection.

    STT  AssemblyAI or Deepgram streaming (turn detection built in) – ai/stt.py
    LLM  Claude / OpenAI / Gemini / any OpenAI-compatible model (app/ai/llm.py)
    TTS  ElevenLabs streaming (μ-law 8 kHz, no transcoding) or Telnyx `speak`

Barge-in: when the caller starts talking while the agent speaks, playback is
cleared and the current answer is cancelled.
"""
import asyncio
import json
import logging
import re
import secrets
import time

from .. import db, outcomes
from ..ai import elevenlabs, llm, stt
from . import carriers

log = logging.getLogger("crm.voice")

FRAME = 160                      # 20 ms of 8 kHz μ-law
PENDING = {}                     # stream token -> session info (created when the call is placed)
LIVE = {}                        # carrier call id -> VoiceSession (for carrier events)

OUTCOMES = ["interested", "not-interested", "callback", "voicemail", "wrong-number", "sale", "other"]

TOOLS = [
    {"name": "end_call", "description": "Hang up after saying goodbye. Use when the conversation is finished, "
     "the person asks to stop, or it is a wrong number.",
     "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
    {"name": "transfer_to_human", "description": "Connect the caller to a human colleague now (hot lead, "
     "complaint, or they ask for a person). Say one short sentence first.",
     "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
    {"name": "save_lead_info", "description": "Store facts learned about the contact (qualification answers, "
     "budget, email, best time…). Call whenever you learn something new.",
     "parameters": {"type": "object", "properties": {"fields": {"type": "object",
                    "description": "field name -> value"}}, "required": ["fields"]}},
    {"name": "schedule_callback", "description": "The contact wants to be called back at a specific time.",
     "parameters": {"type": "object", "properties": {
         "when": {"type": "string", "description": "ISO 8601 date-time in the contact's local time"},
         "note": {"type": "string"}}, "required": ["when"]}},
    {"name": "set_outcome", "description": "Record the result of the call.",
     "parameters": {"type": "object", "properties": {"outcome": {"type": "string", "enum": OUTCOMES}},
                    "required": ["outcome"]}},
]


def new_token(info, token=None):
    token = token or secrets.token_urlsafe(24)
    info["created"] = time.time()
    PENDING[token] = info
    for t, v in list(PENDING.items()):          # forget tokens of calls that never connected
        if time.time() - v["created"] > 900:
            PENDING.pop(t, None)
    return token


def render(text, variables):
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(variables.get(m.group(1), "")), text or "")


def contact_vars(contact):
    c = contact or {}
    name = (c.get("name") or "").strip()
    return {"name": name, "first_name": name.split(" ")[0] if name else "", "company": c.get("company", ""),
            "phone": c.get("phone", ""), "email": c.get("email", ""), "country": c.get("country", ""),
            "notes": c.get("notes", "")}


def build_system(cfg, variables):
    fields = cfg.get("fields") or []
    lines = [
        render(cfg.get("prompt", ""), variables),
        "",
        "You are speaking on a live phone call. Latency-sensitive; begin your visible answer immediately.",
        "Keep every reply to one or two short spoken sentences. No lists, no markdown, no emojis.",
        "Spell numbers, dates and prices the way a person would say them.",
        f"Speak {cfg.get('language_name') or 'the same language as the caller'}.",
        "Use the tools: save_lead_info when you learn something, set_outcome before the call ends, "
        "end_call to hang up, transfer_to_human for a hot lead or when asked for a person"
        + ("" if cfg.get("transfer_to") else " (no human is available right now – offer a callback instead)") + ".",
    ]
    if fields:
        lines.append("Try to learn: " + "; ".join(f"{f['name']} ({f.get('description', '')})" for f in fields))
    known = {k: v for k, v in variables.items() if v}
    if known:
        lines.append("What we know about the contact: " + json.dumps(known, ensure_ascii=False))
    return "\n".join(lines)


class VoiceSession:
    def __init__(self, info, transport):
        self.info = info
        self.transport = transport                  # .send_audio(bytes) .clear() .provider .external_id
        self.call_id = info["call_id"]
        with db.tx() as con:
            a = con.execute("SELECT config FROM ai_agents WHERE id = ?", (info["agent_id"],)).fetchone()
        self.cfg = db.jload(a["config"]) if a else {}
        self.vars = info.get("vars") or {}
        self.system = build_system(self.cfg, self.vars)
        self.history = []
        self.transcript = []
        self.fields, self.outcome, self.callback = {}, "", None
        self.out = asyncio.Queue()
        self.speaking_until = 0.0
        self.reply_task = None
        self.ending = False
        self.closed = False
        self.last_activity = time.time()
        self.started = time.time()
        self.nudged = False
        self.tasks = []
        self.stt = None

    # ------------------------------------------------------------ lifecycle
    async def start(self):
        LIVE[self.transport.external_id] = self
        self.tasks.append(asyncio.create_task(self._player()))
        self.tasks.append(asyncio.create_task(self._watchdog()))
        try:
            self.stt = stt.streaming(self._on_partial, self._on_turn, language=self.cfg.get("language", ""),
                                     override=self.cfg.get("stt_provider", ""))
            await self.stt.start()
        except Exception as e:                       # no STT = no conversation; say so and hang up
            log.warning("STT failed: %s", e)
            self.ending = True                           # no "are you still there?" – just end the call
            await self.say("Sorry, we are having technical difficulties. Goodbye.")
            await self._hangup_after_speech()
            return
        first = render(self.cfg.get("first_message", ""), self.vars)
        if first:
            self.history.append({"role": "assistant", "text": first})
            self.transcript.append({"role": "agent", "text": first})
            await self.say(first)

    async def feed(self, audio: bytes):
        if self.stt:
            await self.stt.send(audio)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        LIVE.pop(self.transport.external_id, None)
        for t in self.tasks + ([self.reply_task] if self.reply_task else []):
            t.cancel()
        if self.stt:
            await self.stt.close()
        await outcomes.finish_ai_conversation(self.call_id, self.transcript, self.fields, self.outcome,
                                              self.callback, int(time.time() - self.started))

    # ------------------------------------------------------------ speech in
    @property
    def speaking(self):
        return time.time() < self.speaking_until or not self.out.empty()

    async def _on_partial(self, text):
        self.last_activity = time.time()
        if self.speaking and len(text.split()) >= 2 and not self.ending:
            await self.interrupt()

    async def _on_turn(self, text):
        self.last_activity = time.time()
        self.nudged = False
        if self.ending:
            return
        await self.interrupt()
        self.transcript.append({"role": "contact", "text": text})
        self.history.append({"role": "user", "text": text})
        self.reply_task = asyncio.create_task(self._reply())

    async def interrupt(self):
        if self.reply_task and not self.reply_task.done():
            self.reply_task.cancel()
        while not self.out.empty():
            self.out.get_nowait()
        self.speaking_until = 0
        try:
            await self.transport.clear()
        except Exception:
            pass

    # ------------------------------------------------------------ thinking
    async def _reply(self):
        try:
            for _ in range(4):                       # LLM -> tools -> LLM … (bounded)
                said, buf, done = "", "", None
                async for kind, data in llm.stream_chat(self.cfg.get("llm_provider", "anthropic"),
                                                        self.cfg.get("llm_model", ""), self.system, self.history,
                                                        TOOLS, effort="low", max_tokens=1024):
                    if kind == "text":
                        buf += data
                        # speak sentence by sentence as soon as each one is complete
                        while True:
                            m = re.search(r"[.!?…]+[\"')\]]*\s", buf)
                            if not m:
                                break
                            sentence, buf = buf[:m.end()].strip(), buf[m.end():]
                            said += sentence + " "
                            await self.say(sentence)
                    else:
                        done = data
                if buf.strip():
                    said += buf.strip()
                    await self.say(buf.strip())
                self.history.append({"role": "assistant", "text": done["text"], "tool_calls": done["tool_calls"],
                                     "raw": done["raw"]})
                if said.strip():
                    self.transcript.append({"role": "agent", "text": said.strip()})
                if not done["tool_calls"]:
                    return
                stop = False
                for call in done["tool_calls"]:
                    result, halt = await self._tool(call["name"], call["input"])
                    self.history.append({"role": "tool", "id": call["id"], "name": call["name"], "result": result})
                    stop = stop or halt
                if stop:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("reply failed: %s", e)
            await self.say("Sorry, could you say that again?")

    async def _tool(self, name, args):
        """Returns (result text for the model, stop generating?)."""
        if name == "save_lead_info":
            if isinstance(args.get("fields"), dict):
                self.fields.update({str(k): v for k, v in args["fields"].items()})
            return "saved", False
        if name == "set_outcome":
            if args.get("outcome") in OUTCOMES:
                self.outcome = args["outcome"]
            return "ok", False
        if name == "schedule_callback":
            self.callback = {"when": str(args.get("when", "")), "note": str(args.get("note", ""))}
            if not self.outcome:
                self.outcome = "callback"
            return "callback noted", False
        if name == "end_call":
            self.ending = True
            asyncio.create_task(self._hangup_after_speech())
            return "hanging up", True
        if name == "transfer_to_human":
            target = self.cfg.get("transfer_to", "")
            if not target:
                return "No human is available. Offer a callback instead.", False
            self.ending = True
            if not self.outcome:
                self.outcome = "interested"
            self.transcript.append({"role": "system", "text": f"Transferred to {target}"})
            asyncio.create_task(self._transfer_after_speech(target))
            return "transferring", True
        return "unknown tool", False

    # ------------------------------------------------------------ speech out
    async def say(self, text):
        text = text.strip()
        if not text:
            return
        if self.cfg.get("tts_provider") == "telnyx" and self.transport.provider == "telnyx":
            await carriers.telnyx_action(self.transport.external_id, "speak", {
                "payload": text, "voice": self.cfg.get("voice_id") or "female",
                "language": self.cfg.get("language") or "en-US"})
            self.speaking_until = time.time() + 0.45 * len(text.split()) + 0.5   # refined by call.speak.ended
            return
        rest = b""
        async for chunk in elevenlabs.tts_stream(text, self.cfg.get("voice_id", ""), self.cfg.get("tts_model", "")):
            data = rest + chunk
            cut = len(data) - len(data) % FRAME
            for i in range(0, cut, FRAME):
                await self.out.put(data[i:i + FRAME])
            rest = data[cut:]
        if rest:
            await self.out.put(rest + b"\xff" * (FRAME - len(rest)))     # pad with μ-law silence

    def carrier_event(self, event_type):
        if event_type == "call.speak.ended":
            self.speaking_until = 0

    async def _player(self):
        """Sends queued audio in real time (20 ms frames) so barge-in can stop it."""
        next_at = time.monotonic()
        while True:
            frame = await self.out.get()
            now = time.monotonic()
            next_at = max(next_at, now)
            await asyncio.sleep(max(0, next_at - now - 0.06))           # stay ~60 ms ahead
            try:
                await self.transport.send_audio(frame)
            except Exception:
                return
            next_at += 0.02
            self.speaking_until = time.time() + max(0, next_at - time.monotonic()) + 0.2
            self.last_activity = time.time()

    async def _drain(self, limit=20):
        t = time.time()
        while self.speaking and time.time() - t < limit:
            await asyncio.sleep(0.1)

    async def _hangup_after_speech(self):
        await self._drain()
        try:
            await carriers.hangup(self.transport.provider, self.transport.external_id)
        except Exception as e:
            log.warning("hangup failed: %s", e)

    async def _transfer_after_speech(self, target):
        await self._drain()
        try:
            await carriers.transfer(self.transport.provider, self.transport.external_id, target,
                                    self.info.get("from_number", ""), self.info.get("trunk_id"))
        except Exception as e:
            log.warning("transfer failed: %s", e)
            self.ending = False
            await self.say("Sorry, I couldn't connect you right now. Someone will call you back shortly.")
            self.outcome = "callback"

    async def _watchdog(self):
        silence = int(self.cfg.get("silence_seconds") or 12)
        max_sec = int(float(self.cfg.get("max_minutes") or 10) * 60)
        while True:
            await asyncio.sleep(1)
            if self.ending:
                continue
            if time.time() - self.started > max_sec:
                self.ending = True
                await self.say("I'm sorry, we have to end the call here. Thank you, goodbye.")
                await self._hangup_after_speech()
                return
            busy = self.speaking or (self.reply_task and not self.reply_task.done())
            if not busy and time.time() - self.last_activity > silence:
                if not self.nudged:
                    self.nudged = True
                    self.last_activity = time.time()
                    await self.say("Are you still there?")
                else:
                    self.ending = True
                    await self.say("I'll let you go. Goodbye.")
                    await self._hangup_after_speech()
                    return
