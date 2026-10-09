"""IntelReach Calling CRM – FastAPI app.

    uvicorn app.main:app --host 127.0.0.1 --port 8040
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import admin, aiapi, api, campaigns, config, db, dialer, hooks, launcher, outcomes, trunks, webhooks
from .voice import audiosocket, media

STATIC = Path(__file__).resolve().parent.parent / "static"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(_app):
    db.init()
    with db.tx() as con:
        trunks.seed_from_env(con)
        trunks.apply(con)
    launcher.fix_agent_carriers()
    outcomes.MAIN_LOOP = asyncio.get_running_loop()
    os.makedirs(os.path.join(config.MEDIA_DIR, "vm"), exist_ok=True)
    task = asyncio.create_task(dialer.run_forever()) if config.RUN_DIALER else None
    sock = await audiosocket.serve()
    yield
    if task:
        task.cancel()
    if sock:
        sock.close()


app = FastAPI(title="IntelReach Calling CRM", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
for r in (api.router, admin.router, campaigns.router, aiapi.router, hooks.router, webhooks.router, media.router):
    app.include_router(r)


@app.middleware("http")
async def no_stale_ui(request, call_next):
    """The web app has no build step: make browsers re-check its files, so an update shows at once."""
    response = await call_next(request)
    if not request.url.path.startswith(("/api/", "/ast/", "/twilio/", "/webhooks/", "/media/", "/public/")):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/health")
def health():
    return {"ok": True, "version": config.VERSION}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


# voicemail audio must be reachable by Telnyx without login (file names are campaign ids only)
os.makedirs(os.path.join(config.MEDIA_DIR, "vm"), exist_ok=True)
app.mount("/public/vm", StaticFiles(directory=os.path.join(config.MEDIA_DIR, "vm")), name="vm")
app.mount("/", StaticFiles(directory=STATIC), name="static")
