"""AssemblyAI: real-time speech-to-text for AI calls, browser tokens for live
captions, and transcripts of recorded calls."""
import asyncio
import json
import logging
import urllib.parse

from .. import net, vault

log = logging.getLogger("crm.assemblyai")

STREAM_URL = "wss://streaming.assemblyai.com/v3/ws"
# Turn detection for phone agents: snappy but tolerant of short pauses (AssemblyAI docs: "aggressive" is 160 / 400,
# "balanced" 400 / 1280). If the API refuses these the stream is opened without them.
TURN_PARAMS = {"end_of_turn_confidence_threshold": 0.4, "min_turn_silence": 160, "max_turn_silence": 700}
API = "https://api.assemblyai.com/v2"


def _key():
    k = vault.key("assemblyai")
    if not k:
        raise net.ProviderError("AssemblyAI API key missing (Admin → Integrations)")
    return k


async def browser_token(seconds=600):
    """Short-lived token so the agent's browser can stream to AssemblyAI directly."""
    async with net.client() as c:
        r = net.check(await c.get("https://streaming.assemblyai.com/v3/token",
                                   params={"expires_in_seconds": seconds}, headers={"Authorization": _key()}),
                      "AssemblyAI")
    return r.json()["token"]


class StreamingSTT:
    """Phone audio (8 kHz μ-law) in, finished user turns out.

    on_partial(text) – words while the caller is still speaking (used for barge-in)
    on_turn(text)    – the caller finished a sentence/turn
    """

    def __init__(self, on_partial, on_turn, sample_rate=8000, encoding="pcm_mulaw", language=""):
        self.on_partial, self.on_turn = on_partial, on_turn
        params = {"sample_rate": sample_rate, "encoding": encoding}
        if language and not language.startswith("en"):
            params["speech_model"] = "universal-streaming-multilingual"
        self.plain_url = STREAM_URL + "?" + urllib.parse.urlencode(params)
        self.url = STREAM_URL + "?" + urllib.parse.urlencode({**params, **TURN_PARAMS})
        self.ws = None
        self.buf = bytearray()
        self.reader = None
        self.send_failed = False

    async def start(self):
        try:
            self.ws = await self._connect(self.url)
        except Exception as e:
            if "400" not in str(e) and "422" not in str(e):
                raise
            log.warning("AssemblyAI refused the turn-detection settings (%s) – using its defaults", e)
            self.ws = await self._connect(self.plain_url)
        self.reader = asyncio.create_task(self._read())

    async def _connect(self, url):
        import websockets
        headers = {"Authorization": _key()}
        try:
            return await websockets.connect(url, additional_headers=headers, max_size=None)
        except TypeError:                     # websockets < 14
            return await websockets.connect(url, extra_headers=headers, max_size=None)

    async def send(self, audio: bytes):
        # AssemblyAI wants 50–1000 ms per message; phone frames are 20 ms
        self.buf += audio
        if self.ws and len(self.buf) >= 800:          # 100 ms of 8 kHz μ-law
            chunk, self.buf = bytes(self.buf), bytearray()
            try:
                await self.ws.send(chunk)
            except Exception as e:
                if not self.send_failed:
                    log.warning("AssemblyAI: sending audio failed: %s", e)
                self.send_failed = True

    async def _read(self):
        try:
            async for msg in self.ws:
                if isinstance(msg, bytes):
                    continue
                data = json.loads(msg)
                if data.get("type") == "Error" or data.get("error"):
                    log.warning("AssemblyAI: %s", msg[:300])
                elif data.get("type") == "Begin":
                    log.info("AssemblyAI session started (%s)", data.get("id", ""))
                if data.get("type") != "Turn":
                    continue
                text = (data.get("transcript") or "").strip()
                if not text:
                    continue
                if data.get("end_of_turn"):
                    await self.on_turn(text)
                else:
                    await self.on_partial(text)
            log.info("AssemblyAI stream closed: %s %s", getattr(self.ws, "close_code", ""), getattr(self.ws, "close_reason", ""))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("AssemblyAI stream error: %s", e)

    async def close(self):
        if self.ws:
            try:
                await self.ws.send(json.dumps({"type": "Terminate"}))
                await self.ws.close()
            except Exception:
                pass
        if self.reader:
            self.reader.cancel()


async def transcribe_file(path_or_url, poll=3.0, timeout=1800):
    """Recording -> [{"role": "speaker A", "text": …}] using speaker labels."""
    headers = {"Authorization": _key()}
    async with net.client(timeout=120) as c:
        if path_or_url.startswith("http"):
            audio_url = path_or_url
        else:
            with open(path_or_url, "rb") as f:
                data = f.read()
            audio_url = net.check(await c.post(f"{API}/upload", content=data, headers=headers), "AssemblyAI").json()["upload_url"]
        job = net.check(await c.post(f"{API}/transcript", headers=headers,
                                     json={"audio_url": audio_url, "speaker_labels": True, "language_detection": True}),
                        "AssemblyAI").json()
        waited = 0.0
        while True:
            t = net.check(await c.get(f"{API}/transcript/{job['id']}", headers=headers), "AssemblyAI").json()
            if t["status"] == "completed":
                break
            if t["status"] == "error":
                raise net.ProviderError(f"AssemblyAI: {t.get('error')}")
            await asyncio.sleep(poll)
            waited += poll
            if waited > timeout:
                raise net.ProviderError("AssemblyAI transcript timed out")
    utts = t.get("utterances") or []
    if utts:
        return [{"role": f"speaker {u.get('speaker', '?')}", "text": u.get("text", "")} for u in utts]
    return [{"role": "call", "text": t.get("text") or ""}]
