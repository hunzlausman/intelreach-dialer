"""LLM providers behind one streaming interface.

    anthropic   – official Anthropic SDK (Claude)
    openai      – OpenAI Chat Completions
    gemini      – Google's OpenAI-compatible endpoint
    custom_llm  – any OpenAI-compatible URL (Groq, DeepSeek, OpenRouter, Together, Ollama, vLLM …)

Conversation history is provider-neutral:
    {"role": "user", "text": "..."}
    {"role": "assistant", "text": "...", "tool_calls": [{"id", "name", "input"}], "raw": [...]}
    {"role": "tool", "id": "...", "name": "...", "result": "..."}
"raw" keeps Claude's own content blocks (thinking, tool_use) so they are sent back unchanged.
Tools are {"name", "description", "parameters": <JSON schema>}.
"""
import json
import re

from .. import net, vault

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5-5",
    "openai": "gpt-4.1-mini",
    "gemini": "gemini-2.5-flash",
    "custom_llm": "",
}
# Claude models that accept server-side refusal fallbacks ("default" routing)
CLAUDE_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
OPENAI_BASES = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}


class LLMError(Exception):
    pass


# ------------------------------------------------------------- public API ----

def _ready(provider):
    cfg = vault.load(provider)
    return bool(cfg.get("base_url")) if provider == "custom_llm" else bool(cfg.get("api_key"))


def resolve(provider, model):
    """The chosen LLM – or, if it has no key, the first one that has (e.g. only a Gemini key is set while
    an agent or Settings still say Anthropic). The model is then that provider's default."""
    if provider in DEFAULT_MODELS and _ready(provider):
        return provider, model
    for p in ("gemini", "anthropic", "openai", "custom_llm"):
        if _ready(p):
            return p, model if p == provider else ""
    return provider, model


async def stream_chat(provider, model, system, history, tools=None, effort="low", max_tokens=2048):
    """Yields ("text", chunk) while generating, then ("done", {"text", "tool_calls", "raw"})."""
    provider, model = resolve(provider, model)
    model = model or DEFAULT_MODELS.get(provider, "")
    if provider == "anthropic":
        async for ev in _anthropic_stream(model, system, history, tools or [], effort, max_tokens):
            yield ev
    elif provider in ("openai", "gemini", "custom_llm"):
        async for ev in _openai_stream(provider, model, system, history, tools or [], max_tokens):
            yield ev
    else:
        raise LLMError(f"Unknown LLM provider '{provider}'")


async def complete(provider, model, system, prompt, effort="medium", max_tokens=16000):
    text = ""
    async for kind, data in stream_chat(provider, model, system, [{"role": "user", "text": prompt}],
                                        effort=effort, max_tokens=max_tokens):
        if kind == "done":
            text = data["text"]
    return text


async def complete_json(provider, model, system, prompt, effort="medium"):
    text = await complete(provider, model, system + "\nReply with one JSON object only, no other text.", prompt, effort)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMError("The model did not return JSON")
    try:
        return json.loads(m.group(0))
    except ValueError as e:
        raise LLMError(f"Invalid JSON from the model: {e}") from e


# -------------------------------------------------------------- Anthropic ----

def to_anthropic(history):
    msgs = []
    for h in history:
        if h["role"] == "user":
            msgs.append({"role": "user", "content": h["text"]})
        elif h["role"] == "assistant":
            if h.get("raw"):
                msgs.append({"role": "assistant", "content": h["raw"]})
            else:
                content = [{"type": "text", "text": h["text"]}] if h.get("text") else []
                content += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
                            for c in h.get("tool_calls") or []]
                msgs.append({"role": "assistant", "content": content or [{"type": "text", "text": "…"}]})
        elif h["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": h["id"], "content": h["result"]}
            # all results of one assistant turn go back in ONE user message
            if msgs and msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], list) \
                    and msgs[-1]["content"] and msgs[-1]["content"][0].get("type") == "tool_result":
                msgs[-1]["content"].append(block)
            else:
                msgs.append({"role": "user", "content": [block]})
    return msgs


def _valid_input(tool_spec, data):
    if not isinstance(data, dict):
        return False
    return all(k in data for k in (tool_spec or {}).get("parameters", {}).get("required", []))


async def _anthropic_stream(model, system, history, tools, effort, max_tokens):
    import anthropic

    key = vault.key("anthropic")
    if not key:
        raise LLMError("Anthropic API key missing (Admin → Integrations)")
    client = anthropic.AsyncAnthropic(api_key=key, http_client=net.client(timeout=120))
    specs = {t["name"]: t for t in tools}
    kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=to_anthropic(history))
    if tools:
        kwargs["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"],
                            "eager_input_streaming": True} for t in tools]
    body = {"output_config": {"effort": effort}}
    headers = {}
    if model in CLAUDE_FALLBACK_MODELS:      # re-run a declined request on Anthropic's recommended fallback
        body["fallbacks"] = "default"
        headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
    final = None
    async with client:
        for attempt in range(2):
            try:
                async with client.messages.stream(**kwargs, extra_body=body, extra_headers=headers) as stream:
                    async for text in stream.text_stream:
                        yield ("text", text)
                    final = await stream.get_final_message()
                break
            except ValueError:               # unparseable streamed tool input: re-issue the turn once
                if attempt:
                    raise LLMError("Claude returned an unreadable tool call") from None
            except anthropic.RateLimitError as e:
                raise LLMError("Claude rate limit – try again shortly") from e
            except anthropic.APIStatusError as e:
                raise LLMError(f"Claude API {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise LLMError("Could not reach the Claude API") from e
    if final.stop_reason == "refusal":
        yield ("done", {"text": "Sorry, I can't help with that.", "tool_calls": [], "raw": None})
        return
    text = "".join(b.text for b in final.content if b.type == "text")
    calls = []
    if final.stop_reason != "max_tokens":
        calls = [{"id": b.id, "name": b.name, "input": b.input} for b in final.content
                 if b.type == "tool_use" and _valid_input(specs.get(b.name), b.input)]
    raw = [b.model_dump(exclude_none=True) for b in final.content]
    yield ("done", {"text": text, "tool_calls": calls, "raw": raw})


# ------------------------------------------------------ OpenAI-compatible ----

def to_openai(system, history):
    msgs = [{"role": "system", "content": system}] if system else []
    for h in history:
        if h["role"] == "user":
            msgs.append({"role": "user", "content": h["text"]})
        elif h["role"] == "assistant":
            m = {"role": "assistant", "content": h.get("text") or None}
            if h.get("tool_calls"):
                m["tool_calls"] = [{"id": c["id"], "type": "function",
                                    "function": {"name": c["name"], "arguments": json.dumps(c["input"])}}
                                   for c in h["tool_calls"]]
            msgs.append(m)
        elif h["role"] == "tool":
            msgs.append({"role": "tool", "tool_call_id": h["id"], "content": h["result"]})
    return msgs


def _openai_target(provider):
    cfg = vault.load(provider)
    base = (cfg.get("base_url") or OPENAI_BASES.get(provider, "")).rstrip("/")
    if not base:
        raise LLMError("Base URL missing for the OpenAI-compatible provider (Admin → Integrations)")
    if not cfg.get("api_key") and provider != "custom_llm":
        raise LLMError(f"{vault.PROVIDERS[provider]['label']} API key missing (Admin → Integrations)")
    return base, cfg.get("api_key", "")


async def _openai_stream(provider, model, system, history, tools, max_tokens):
    base, key = _openai_target(provider)
    body = {"model": model, "messages": to_openai(system, history), "stream": True, "max_tokens": max_tokens}
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                           "parameters": t["parameters"]}} for t in tools]
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    text, calls = "", {}
    async with net.client(timeout=120) as c:
        async with c.stream("POST", f"{base}/chat/completions", json=body, headers=headers) as r:
            if r.status_code >= 400:
                await r.aread()
                net.check(r, provider)
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                for choice in chunk.get("choices") or []:
                    d = choice.get("delta") or {}
                    if d.get("content"):
                        text += d["content"]
                        yield ("text", d["content"])
                    for tc in d.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] = fn.get("name") or slot["name"]
                        slot["args"] += fn.get("arguments") or ""
    out = []
    for i in sorted(calls):
        s = calls[i]
        try:
            args = json.loads(s["args"] or "{}")
        except ValueError:
            continue
        out.append({"id": s["id"] or f"call_{i}", "name": s["name"], "input": args})
    yield ("done", {"text": text, "tool_calls": out, "raw": None})
