"""Deepgram: real-time speech-to-text for AI calls, browser tokens for live
captions, and transcripts of recorded calls (same interface as assemblyai.py)."""
import asyncio
import json
import mimetypes
import urllib.parse

from .. import net, vault

API = "https://api.deepgram.com/v1"
STREAM_URL = "wss://api.deepgram.com/v1/listen"
DEFAULT_MODEL = "nova-3"


def _cfg():
    cfg = vault.load("deepgram")
    if not cfg.get("api_key"):
        raise net.ProviderError("Deepgram API key missing (Admin → Integrations)")
    return cfg


def _language(cfg, language=""):
    """Agent language (en-US, es …) wins; else the integration's; '' = multilingual / auto-detect."""
    return (language or cfg.get("language") or "").strip()


def stream_params(cfg, sample_rate, encoding, language=""):
    lang = _language(cfg, language)
    return {"model": cfg.get("model") or DEFAULT_MODEL, "encoding": encoding, "sample_rate": sample_rate,
            "channels": 1, "language": lang or "multi", "interim_results": "true", "smart_format": "true",
            "endpointing": 300, "utterance_end_ms": 1000, "vad_events": "true"}


async def browser_token(seconds=300):
    """Short-lived JWT so the agent's browser can stream to Deepgram directly (checked only when the
    WebSocket opens). The key needs the Member role or higher to grant tokens."""
    cfg = _cfg()
    async with net.client() as c:
        r = net.check(await c.post(f"{API}/auth/grant", json={"ttl_seconds": int(seconds)},
                                   headers={"Authorization": f"Token {cfg['api_key']}"}), "Deepgram")
    return r.json()["access_token"]


async def check_key():
    cfg = _cfg()
    async with net.client() as c:
        r = net.check(await c.get(f"{API}/projects", headers={"Authorization": f"Token {cfg['api_key']}"}), "Deepgram")
    return [p.get("name", "") for p in r.json().get("projects") or []]


class Turns:
    """Deepgram sends interim and final pieces of a sentence; this joins them into
    on_partial (still speaking) and on_turn (finished) like AssemblyAI's turns."""

    def __init__(self, on_partial, on_turn):
        self.on_partial, self.on_turn = on_partial, on_turn
        self.done = []

    async def handle(self, data):
        if data.get("type") == "UtteranceEnd":
            return await self._flush()
        if data.get("type") != "Results":
            return
        alts = (data.get("channel") or {}).get("alternatives") or [{}]
        text = (alts[0].get("transcript") or "").strip()
        if data.get("is_final"):
            if text:
                self.done.append(text)
            if data.get("speech_final"):
                await self._flush()
            elif self.done:
                await self.on_partial(" ".join(self.done))
        elif text:
            await self.on_partial(" ".join(self.done + [text]))

    async def _flush(self):
        if self.done:
            text, self.done = " ".join(self.done), []
            await self.on_turn(text)


class StreamingSTT:
    """Phone audio (8 kHz μ-law) in, finished user turns out – see assemblyai.StreamingSTT."""

    def __init__(self, on_partial, on_turn, sample_rate=8000, encoding="mulaw", language=""):
        self.turns = Turns(on_partial, on_turn)
        self.cfg = _cfg()
        self.url = STREAM_URL + "?" + urllib.parse.urlencode(stream_params(self.cfg, sample_rate, encoding, language))
        self.ws = None
        self.buf = bytearray()
        self.tasks = []

    async def start(self):
        import websockets
        headers = {"Authorization": f"Token {self.cfg['api_key']}"}
        try:
            self.ws = await websockets.connect(self.url, additional_headers=headers, max_size=None)
        except TypeError:                     # websockets < 14
            self.ws = await websockets.connect(self.url, extra_headers=headers, max_size=None)
        self.tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._keepalive())]

    async def send(self, audio: bytes):
        self.buf += audio
        if self.ws and len(self.buf) >= 800:          # 100 ms of 8 kHz μ-law
            chunk, self.buf = bytes(self.buf), bytearray()
            try:
                await self.ws.send(chunk)
            except Exception:
                pass

    async def _keepalive(self):
        # Deepgram closes a stream after ~10 s without audio (e.g. while the call is on hold)
        try:
            while True:
                await asyncio.sleep(5)
                await self.ws.send(json.dumps({"type": "KeepAlive"}))
        except Exception:
            pass

    async def _read(self):
        try:
            async for msg in self.ws:
                if isinstance(msg, bytes):
                    continue
                await self.turns.handle(json.loads(msg))
        except Exception:
            pass

    async def close(self):
        if self.ws:
            try:
                await self.ws.send(json.dumps({"type": "CloseStream"}))
                await self.ws.close()
            except Exception:
                pass
        for t in self.tasks:
            t.cancel()


async def transcribe_file(path_or_url, timeout=1800):
    """Recording -> [{"role": "speaker 0", "text": …}] using diarization."""
    cfg = _cfg()
    lang = _language(cfg)
    params = {"model": cfg.get("model") or DEFAULT_MODEL, "smart_format": "true", "diarize": "true",
              "utterances": "true", "punctuate": "true"}
    params.update({"language": lang} if lang else {"detect_language": "true"})
    headers = {"Authorization": f"Token {cfg['api_key']}"}
    async with net.client(timeout=timeout) as c:
        if path_or_url.startswith("http"):
            r = await c.post(f"{API}/listen", params=params, headers=headers, json={"url": path_or_url})
        else:
            with open(path_or_url, "rb") as f:
                data = f.read()
            headers["Content-Type"] = mimetypes.guess_type(path_or_url)[0] or "audio/wav"
            r = await c.post(f"{API}/listen", params=params, headers=headers, content=data)
    res = net.check(r, "Deepgram").json().get("results") or {}
    utts = res.get("utterances") or []
    if utts:
        return [{"role": f"speaker {u.get('speaker', '?')}", "text": u.get("transcript", "")} for u in utts
                if u.get("transcript")]
    alts = ((res.get("channels") or [{}])[0].get("alternatives") or [{}])
    return [{"role": "call", "text": alts[0].get("transcript") or ""}]
