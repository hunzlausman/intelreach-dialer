"""One place to create HTTP clients, so tests can swap in httpx.MockTransport."""
import httpx2 as httpx

transport = None          # tests: net.transport = httpx.MockTransport(handler)


def client(timeout=20, **kw):
    if transport is not None:
        kw["transport"] = transport
    return httpx.AsyncClient(timeout=timeout, **kw)


class ProviderError(Exception):
    pass


def check(r, who):
    if r.status_code >= 400:
        try:
            body = r.json()
            if isinstance(body, list) and body:          # Gemini wraps errors: [{"error": {...}}]
                body = body[0]
            if isinstance(body, dict) and isinstance((body.get("metadata") or {}).get("errors"), list):
                body = {"message": "; ".join(map(str, body["metadata"]["errors"]))}   # AssemblyAI LLM Gateway
            if isinstance(body, dict):
                err = body.get("error")
                msg = (body.get("detail") or body.get("message") or body.get("errors")
                       or (err.get("message") or err.get("status") if isinstance(err, dict) else err) or body)
            else:
                msg = body
        except ValueError:
            msg = r.text[:300]
        detail = str(msg)
        if r.status_code == 400 and len(detail) < 60 and len(r.text) > len(detail) + 20:   # vague: add the raw body
            detail += " – " + r.text[:400]
        raise ProviderError(f"{who} {r.status_code}: {detail[:500]}")
    return r
