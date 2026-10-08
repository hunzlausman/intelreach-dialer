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
            msg = (body.get("detail") or body.get("message") or body.get("errors") or body.get("error") or body)
        except ValueError:
            msg = r.text[:300]
        raise ProviderError(f"{who} {r.status_code}: {str(msg)[:300]}")
    return r
