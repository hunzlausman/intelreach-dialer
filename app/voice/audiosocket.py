"""AI agents on your own SIP trunks: Asterisk <-> voice engine over AudioSocket.

Outbound:  originate() drops a call file into Asterisk's spool -> Asterisk dials
           PJSIP/<number>@crm-trunk-<id> -> on answer the dialplan (context crm-ai)
           runs Dial(AudioSocket/127.0.0.1:<port>/<uuid>) -> serve() below.
Inbound:   /ast/inbound or /ast/ai-start hands the call to crm-ai the same way.

AudioSocket (Asterisk 18+, chan_audiosocket): TCP, messages of
    1 byte kind | 2 bytes length (big endian) | payload
    0x00 hangup, 0x01 uuid (16 bytes), 0x10 audio (signed 16-bit 8 kHz mono, LE), 0xff error
The engine speaks 8 kHz μ-law (like Telnyx / Twilio streams), so frames are converted here.
Only 127.0.0.1 can connect, and only with a uuid the CRM created for a call it placed / accepted.
"""
import asyncio
import logging
import os
import struct
import uuid as uuidlib

from .. import config
from . import engine

log = logging.getLogger("crm.audiosocket")

KIND_HANGUP, KIND_UUID, KIND_AUDIO, KIND_ERROR = 0x00, 0x01, 0x10, 0xFF
CONNS = {}          # uuid -> Transport of a live AI call
NEXT = {}           # call id -> {"target", "caller_id", "trunk_id"}: what the dialplan does after the AI hangs up
PORT = None         # the port actually listening (tests use an ephemeral one)


# ------------------------------------------------------------------ G.711 μ-law ----

def _ulaw_to_lin(u):
    u = ~u & 0xFF
    exp, mant = (u >> 4) & 7, u & 0x0F
    s = (((mant << 3) + 0x84) << exp) - 0x84
    return -s if u & 0x80 else s


def _lin_to_ulaw(s):
    sign = 0x80 if s < 0 else 0
    s = min(abs(s), 32635) + 0x84
    exp, mask = 7, 0x4000
    while exp > 0 and not s & mask:
        exp, mask = exp - 1, mask >> 1
    return ~(sign | (exp << 4) | ((s >> (exp + 3)) & 0x0F)) & 0xFF


_DEC = [struct.pack("<h", _ulaw_to_lin(i)) for i in range(256)]
_ENC = bytes(_lin_to_ulaw(i - 65536 if i > 32767 else i) for i in range(65536))


def ulaw_to_pcm(data: bytes) -> bytes:
    return b"".join(_DEC[b] for b in data)


def pcm_to_ulaw(data: bytes) -> bytes:
    n = len(data) // 2
    return bytes(_ENC[s & 0xFFFF] for s in struct.unpack(f"<{n}h", data[:n * 2]))


# ------------------------------------------------------------------- transport ----

class Transport:
    """What engine.VoiceSession talks to (same shape as the Telnyx / Twilio WebSocket transport)."""
    provider = "sip"

    def __init__(self, writer, uid, call_id):
        self.writer, self.external_id, self.call_id = writer, uid, call_id

    async def send_audio(self, frame: bytes):
        pcm = ulaw_to_pcm(frame)
        self.writer.write(bytes([KIND_AUDIO]) + len(pcm).to_bytes(2, "big") + pcm)
        await self.writer.drain()

    async def clear(self):
        pass                # the engine sends in real time, so nothing is queued on Asterisk's side

    def hangup(self):
        try:
            self.writer.write(bytes([KIND_HANGUP, 0, 0]))
            self.writer.close()
        except Exception:
            pass


def hangup(uid):
    t = CONNS.get(uid)
    if t:
        t.hangup()


def transfer(uid, target, caller_id="", trunk_id=None):
    """Ends the AI leg; the dialplan then asks /ast/ai-next and dials the target."""
    t = CONNS.get(uid)
    if not t:
        raise RuntimeError("call is no longer connected")
    NEXT[t.call_id] = {"target": target, "caller_id": caller_id, "trunk_id": trunk_id}
    t.hangup()


def active(call_id):
    return any(t.call_id == call_id for t in CONNS.values())


async def _read(reader, timeout=None):
    head = await asyncio.wait_for(reader.readexactly(3), timeout)
    n = int.from_bytes(head[1:], "big")
    return head[0], (await reader.readexactly(n) if n else b"")


async def _handle(reader, writer):
    session, uid, t, frames = None, None, None, 0
    try:
        kind, payload = await _read(reader, timeout=5)
        if kind != KIND_UUID or len(payload) != 16:
            return
        uid = str(uuidlib.UUID(bytes=payload))
        info = engine.PENDING.pop(uid, None)
        if not info:
            log.warning("AudioSocket connection with an unknown call id %s", uid)
            return
        t = Transport(writer, uid, info["call_id"])
        CONNS[uid] = t
        log.info("AI call %s connected (AudioSocket %s)", info["call_id"], uid)
        session = engine.VoiceSession(info, t)
        await session.start()
        while True:
            kind, payload = await _read(reader)
            if kind == KIND_AUDIO and payload:
                frames += 1
                await session.feed(pcm_to_ulaw(payload))
            elif kind in (KIND_HANGUP, KIND_ERROR):
                break
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        pass
    except Exception as e:
        log.warning("AudioSocket error: %s", e)
    finally:
        if t is not None:
            log.info("AI call %s ended – %s audio frames (%.1f s) received from the caller", t.call_id, frames, frames * 0.02)
        if t is not None and CONNS.get(uid) is t:       # a refused duplicate must not drop the live call's entry
            CONNS.pop(uid, None)
        if session:
            await session.close()
        try:
            writer.close()
        except Exception:
            pass


async def serve():
    """Started with the app. Returns the server, or None if the port is taken (AI over SIP then unavailable)."""
    global PORT
    try:
        server = await asyncio.start_server(_handle, "127.0.0.1", config.AUDIOSOCKET_PORT)
    except OSError as e:
        log.warning("AudioSocket server not started on 127.0.0.1:%s: %s", config.AUDIOSOCKET_PORT, e)
        return None
    PORT = server.sockets[0].getsockname()[1]
    return server


# ------------------------------------------------------------------- outbound ----

def originate(endpoint, dial, caller_id, call_id, uid, wait=45):
    """Asterisk call file: dial out through the trunk; on answer -> context crm-ai (extensions_crm.conf)."""
    text = (f"Channel: PJSIP/{dial}@{endpoint}\nCallerID: <{caller_id}>\nMaxRetries: 0\nWaitTime: {int(wait)}\n"
            f"Context: crm-ai\nExtension: s\nPriority: 1\nSetvar: CRMID={int(call_id)}\nSetvar: AIUUID={uid}\n"
            f"Archive: no\n")
    # Asterisk dials every file that appears in its spool – even a half-written or temporary one – so the
    # file is written outside the spool and moved in with one rename (a dot-name inside it was dialled twice)
    final = os.path.join(config.AST_SPOOL, f"crm-ai-{int(call_id)}.call")
    tmp = os.path.join(config.AST_SPOOL_TMP, f"crm-ai-{int(call_id)}.call")
    try:
        try:
            with open(tmp, "w", encoding="ascii") as f:
                f.write(text)
            os.chmod(tmp, 0o664)
            os.replace(tmp, final)
        except OSError:
            if os.path.exists(tmp):
                os.unlink(tmp)
            with open(final, "w", encoding="ascii") as f:     # no usable temp dir: Asterisk waits for the close
                f.write(text)
    except OSError as e:
        raise RuntimeError(f"cannot write to Asterisk's spool {config.AST_SPOOL} ({e}) – re-run deploy/install.sh")
