"""Speech-to-text provider choice: AssemblyAI or Deepgram.

Admin → Settings → Speech-to-text picks the provider for live captions and recording
transcripts (and AI agents, unless an agent chooses its own)."""
from .. import db, vault
from . import assemblyai, deepgram

PROVIDERS = {"assemblyai": "AssemblyAI", "deepgram": "Deepgram"}


def provider(override=""):
    if override in PROVIDERS:
        return override
    with db.tx() as con:
        p = db.get_settings(con).get("stt_provider", "assemblyai")
    return p if p in PROVIDERS else "assemblyai"


def streaming(on_partial, on_turn, language="", override=""):
    """Phone-audio STT for the AI voice engine (8 kHz μ-law)."""
    if provider(override) == "deepgram":
        return deepgram.StreamingSTT(on_partial, on_turn, language=language)
    return assemblyai.StreamingSTT(on_partial, on_turn, language=language)


async def transcribe_file(path_or_url):
    if provider() == "deepgram":
        return await deepgram.transcribe_file(path_or_url)
    return await assemblyai.transcribe_file(path_or_url)


async def browser_session():
    """What the agent's browser needs to stream live captions itself."""
    if provider() == "deepgram":
        cfg = vault.load("deepgram")
        return {"provider": "deepgram", "token": await deepgram.browser_token(300),
                "params": deepgram.stream_params(cfg, 16000, "linear16")}
    return {"provider": "assemblyai", "token": await assemblyai.browser_token(3600)}
