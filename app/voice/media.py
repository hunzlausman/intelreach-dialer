"""Media-stream WebSockets: Telnyx and Twilio send the caller's audio here and
play whatever the VoiceSession sends back.

    wss://crm.intelreach.com/media/telnyx/<token>
    wss://crm.intelreach.com/media/twilio/<token>

The token is created when the CRM places / accepts the call (engine.new_token),
so a random connection cannot start a session.
"""
import base64
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import db
from .engine import PENDING, VoiceSession

log = logging.getLogger("crm.voice")
router = APIRouter()


class Transport:
    def __init__(self, ws, provider):
        self.ws, self.provider = ws, provider
        self.external_id = ""
        self.stream_sid = ""

    async def send_audio(self, frame: bytes):
        payload = base64.b64encode(frame).decode()
        if self.provider == "twilio":
            await self.ws.send_text(json.dumps({"event": "media", "streamSid": self.stream_sid,
                                                "media": {"payload": payload}}))
        else:
            await self.ws.send_text(json.dumps({"event": "media", "media": {"payload": payload}}))

    async def clear(self):
        msg = {"event": "clear"}
        if self.provider == "twilio":
            msg["streamSid"] = self.stream_sid
        await self.ws.send_text(json.dumps(msg))


async def _run(ws: WebSocket, provider: str, token: str):
    info = PENDING.pop(token, None)
    await ws.accept()
    if not info:
        await ws.close(code=4403)
        return
    t = Transport(ws, provider)
    session = None
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            ev = msg.get("event")
            if ev == "start" and not session:
                start = msg.get("start") or {}
                if provider == "twilio":
                    t.stream_sid = msg.get("streamSid") or start.get("streamSid", "")
                    t.external_id = start.get("callSid", "")
                else:
                    t.external_id = start.get("call_control_id") or info.get("external_id", "")
                if t.external_id:
                    with db.tx() as con:
                        con.execute("UPDATE calls SET external_id = ?, status = 'answered' WHERE id = ?",
                                    (t.external_id, info["call_id"]))
                session = VoiceSession(info, t)
                await session.start()
            elif ev == "media" and session:
                media = msg.get("media") or {}
                if media.get("track", "inbound") in ("inbound", "inbound_track"):
                    await session.feed(base64.b64decode(media.get("payload", "")))
            elif ev == "stop":
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("media stream error: %s", e)
    finally:
        if session:
            await session.close()


@router.websocket("/media/telnyx/{token}")
async def telnyx_media(ws: WebSocket, token: str):
    await _run(ws, "telnyx", token)


@router.websocket("/media/twilio/{token}")
async def twilio_media(ws: WebSocket, token: str):
    await _run(ws, "twilio", token)
