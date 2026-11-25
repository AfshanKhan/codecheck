"""Pre-flight check of an LLM provider key and model name: a typo or a key without access fails fast
with a clear message, instead of every file in the review being skipped."""

from __future__ import annotations

import difflib
import ipaddress
import re
import socket
from urllib.parse import urlsplit

import httpx

from codecheck.reviewers.cloud_llm import (
    _ANTHROPIC_API_URL,
    _ANTHROPIC_VERSION,
    _OPENAI_COMPATIBLE_PRESETS,
)

TIMEOUT = 20.0


def _norm(model: str) -> str:
    return model.removeprefix("models/")


def _provider_message(r: httpx.Response, key: str) -> str:
    """The provider's own explanation, trimmed (error bodies differ: {"error": {...}} or [{"error": {...}}])."""
    try:
        body = r.json()
        body = body[0] if isinstance(body, list) and body else body
        err = body.get("error", body) if isinstance(body, dict) else body
        text = err.get("message") if isinstance(err, dict) else str(err)
    except ValueError:
        text = r.text
    return (text or "").replace(key, "***").strip().replace("\n", " ")[:220]


def _bad_key(r: httpx.Response) -> bool:
    """401/403, or Google's HTTP 400 "API key not valid"."""
    return r.status_code in (401, 403) or (r.status_code == 400 and "api key" in r.text.lower())


def _headers(provider: str, key: str) -> dict[str, str]:
    if provider == "anthropic":
        return {"x-api-key": key, "anthropic-version": _ANTHROPIC_VERSION}
    return {"authorization": f"Bearer {key}"}


def _models_url(provider: str) -> str:
    if provider == "anthropic":
        return "https://api.anthropic.com/v1/models?limit=1000"
    return _OPENAI_COMPATIBLE_PRESETS[provider]["base_url"].replace("/chat/completions", "/models")


_NOT_CHAT = re.compile(
    r"embed|whisper|tts|guard|moderat|imagen|veo|aqa|ocr|transcri|audio|image|dall|rerank|robotics|live|"
    r"banana|lyria|genie|computer-use|deep-research|diffusion|flux|sora|stable|video|speech|voice", re.I)
_ONLY_THESE = {"google": re.compile(r"^(gemini|gemma)-", re.I)}   # Google's list also holds image, video and music models


def _text_output(model: dict) -> bool:
    arch = model.get("architecture")
    out = arch.get("output_modalities") if isinstance(arch, dict) else None
    return not isinstance(out, list) or out == ["text"] or ("text" in out and len(out) == 1)


def _data_rows(response: httpx.Response) -> list[dict]:
    """Return validated model-entry dicts from a JSON `{"data": [...]}` response."""
    try:
        payload = response.json()
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [m for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)]


def list_models(provider: str, key: str, client: httpx.Client | None = None) -> tuple[list[str], str | None]:
    """(chat model IDs the key can see, error). The IDs come from the provider's own model list."""
    if provider != "anthropic" and provider not in _OPENAI_COMPATIBLE_PRESETS:
        return [], f"Unknown provider {provider!r}."
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        r = client.get(_models_url(provider), headers=_headers(provider, key))
    except httpx.HTTPError as e:
        return [], f"Could not reach {provider}: {type(e).__name__}."
    finally:
        if own:
            client.close()
    if _bad_key(r):
        return [], f"{provider} rejected this API key (HTTP {r.status_code})."
    if r.status_code != 200:
        return [], f"{provider} answered HTTP {r.status_code} when listing models."
    rows = _data_rows(r)
    only = _ONLY_THESE.get(provider)
    ids = sorted({
        _norm(m["id"]) for m in rows
        if not _NOT_CHAT.search(m["id"]) and (only is None or only.search(_norm(m["id"])))
        and _text_output(m)       # OpenRouter says what a model outputs: keep text-only ones
    })
    return ids, None if ids else f"{provider} returned no models for this key."


def check(provider: str, model: str, key: str, client: httpx.Client | None = None) -> str | None:
    """None when the key can use the model, otherwise a message a person can act on."""
    if provider != "anthropic" and provider not in _OPENAI_COMPATIBLE_PRESETS:
        return f"Unknown provider {provider!r}."
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    headers = _headers(provider, key)
    try:
        # 1. Does the model exist for this provider? (best effort: not every provider lists models)
        try:
            r = client.get(_models_url(provider), headers=headers)
        except httpx.HTTPError as e:
            return f"Could not reach {provider}: {type(e).__name__}."
        if _bad_key(r):
            return f"{provider} rejected this API key (HTTP {r.status_code}). Check it was copied in full and is still active."
        if r.status_code == 200:
            ids = [_norm(m["id"]) for m in _data_rows(r)]
            if ids and _norm(model) not in ids:
                close = difflib.get_close_matches(_norm(model), ids, n=4, cutoff=0.5) or sorted(ids)[:5]
                return f'{provider} has no model called "{model}". Closest names: {", ".join(close)}.'
            if ids:
                return None  # listed, so skip the probe: free keys allow only a handful of requests a day
        # 2. Listing unavailable: can this key use it? One tiny request.
        if provider == "anthropic":
            url, payload = _ANTHROPIC_API_URL, {"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
        else:
            url = _OPENAI_COMPATIBLE_PRESETS[provider]["base_url"]
            payload = {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
        try:
            r = client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as e:
            return f"Could not reach {provider}: {type(e).__name__}."
        if r.status_code == 200:
            return None
        why = _provider_message(r, key)
        if r.status_code == 400 and _bad_key(r):
            return f"{provider} rejected this API key (HTTP 400). {why}"
        return {
            401: f"{provider} rejected this API key (HTTP 401). {why}",
            403: f'This key cannot use "{model}" (HTTP 403). It may lack access, billing or a region. {why}',
            404: f'{provider} does not know the model "{model}" (HTTP 404). Check the spelling. {why}',
            429: f"The key is out of quota or rate limited (HTTP 429). {why}",
            400: f"{provider} rejected the request (HTTP 400). {why}",
        }.get(r.status_code, f"{provider} answered HTTP {r.status_code}. {why}")
    finally:
        if own:
            client.close()


def _public(host: str, port: int) -> bool:
    """True when every address the host resolves to is a public one (not loopback, LAN, link-local or CGNAT/Tailscale)."""
    try:
        addrs = {i[4][0] for i in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)}
        return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)
    except (OSError, ValueError):
        return False


def local_root(url: str) -> tuple[str | None, str | None]:
    """(server root, error) for a model server the user runs themselves. This server only ever talks to https
    URLs on public addresses, so a visitor cannot point it at the machine's own network."""
    try:
        parts = urlsplit(url.strip())
        host, port = parts.hostname, parts.port or 443
    except ValueError:
        return None, "That is not a valid URL."
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return None, "Use the https:// address a tunnel gives you, with no username or password in it."
    try:
        socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return None, f"Could not find {host}. Check the address, and that your tunnel is running."
    if not _public(host, port):
        return None, ("That address points to a private or local network, which this server cannot reach. "
                      "Expose your model with a public https tunnel and paste that address.")
    path = re.sub(r"/v1(/chat/completions|/models)?/?$", "", parts.path.rstrip("/"))
    return f"https://{parts.netloc}{path}", None


def _names(ids: list[str]) -> str:
    return ", ".join(ids[:6]) + (f" and {len(ids) - 6} more" if len(ids) > 6 else "")


def _loaded(client: httpx.Client, root: str, ids: list[str]) -> list[str] | None:
    """The models actually loaded, from Ollama's /api/ps or LM Studio's /api/v0/models; None if neither answers."""
    try:
        r = client.get(root + "/api/ps")
        if r.status_code == 200:
            payload = r.json()
            models = payload.get("models") if isinstance(payload, dict) else None
            if isinstance(models, list):
                names = {
                    name
                    for m in models
                    if isinstance(m, dict)
                    for name in (m.get("model") or m.get("name"),)
                    if isinstance(name, str)
                }
                return [i for i in ids if i in names]
    except (httpx.HTTPError, ValueError):
        pass
    try:
        r = client.get(root + "/api/v0/models")
        if r.status_code == 200:
            payload = r.json()
            if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                return [
                    m["id"]
                    for m in _data_rows(r)
                    if m.get("state") == "loaded" and m.get("type", "llm") in ("llm", "vlm")
                ]
    except (httpx.HTTPError, ValueError):
        pass
    return None


def detect_model(root: str, chosen: str | None = None, client: httpx.Client | None = None) -> tuple[str | None, str | None, list[str]]:
    """(model, error, choices): the model the user's server is serving, so nobody types a name and misspells it.
    One model on the server, or exactly one loaded, is used. Several loaded: `choices` lists them for the page to
    offer, unless `chosen` is one of them. Otherwise `error` says how to fix it on their machine."""
    host = urlsplit(root).hostname
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        try:
            r = client.get(root + "/v1/models")
        except httpx.HTTPError as e:
            return None, f"Could not reach your model server at {host} ({type(e).__name__}). Is Ollama or LM Studio running, and the tunnel up?", []
        if r.status_code == 403:
            return None, ("Your server refused the request (HTTP 403). For Ollama behind cloudflared, start the tunnel with "
                          "--http-host-header=localhost:11434."), []
        if r.status_code in (502, 503, 504, 530):
            return None, f"The tunnel answered but nothing is answering behind it (HTTP {r.status_code}). Is Ollama or LM Studio running?", []
        if r.status_code != 200:
            return None, f"Your server answered HTTP {r.status_code} to the model list. Is this the address of an Ollama or LM Studio server?", []
        ids = [m["id"] for m in _data_rows(r)]
        if not ids:
            return None, "Your server has no models. Download one (Ollama: ollama pull qwen2.5-coder:7b, or in LM Studio), load it and try again.", []
        if chosen:
            return (chosen, None, []) if chosen in ids else (None, None, ids)
        if len(ids) == 1:
            return ids[0], None, []
        loaded = _loaded(client, root, ids)
        if loaded is None:
            return None, None, ids                        # cannot tell which is loaded: let the user pick
        if len(loaded) == 1:
            return loaded[0], None, []
        if not loaded:
            return None, (f"No model is loaded on your server. Please load one (Ollama: ollama run <name>, then leave it; "
                          f"LM Studio: load a model in the app) and try again. Models on your server: {_names(ids)}."), []
        return None, None, loaded
    finally:
        if own:
            client.close()
