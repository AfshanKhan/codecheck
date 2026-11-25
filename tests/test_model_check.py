import httpx

from codecheck.model_check import check


def client(routes):
    """routes: {(method, path-suffix): (status, json)}; anything else is a 599."""
    def handler(req: httpx.Request) -> httpx.Response:
        for (method, suffix), (status, body) in routes.items():
            if req.method == method and req.url.path.endswith(suffix):
                return httpx.Response(status, json=body)
        return httpx.Response(599, json={})
    return httpx.Client(transport=httpx.MockTransport(handler))


LIST = {"data": [{"id": "models/gemini-3.6-flash"}, {"id": "models/gemini-3.6-pro"}, {"id": "models/gemma-4"}]}
OK = (200, {"choices": []})


def test_ok_when_model_listed_and_probe_works():
    c = client({("GET", "/models"): (200, LIST), ("POST", "/chat/completions"): OK})
    assert check("google", "gemini-3.6-flash", "k", c) is None
    assert check("google", "models/gemini-3.6-flash", "k", c) is None


def test_typo_suggests_closest_names():
    c = client({("GET", "/models"): (200, LIST), ("POST", "/chat/completions"): OK})
    msg = check("google", "gemini-3.6-flsh", "k", c)
    assert 'no model called "gemini-3.6-flsh"' in msg and "gemini-3.6-flash" in msg


def test_bad_key_is_named_as_the_key():
    c = client({("GET", "/models"): (401, {"error": {"message": "bad key"}})})
    assert "rejected this API key" in check("groq", "m", "k", c)


def test_no_access_to_model_shows_provider_reason_without_the_key():
    c = client({("GET", "/models"): (404, {}), ("POST", "/chat/completions"): (403, [{"error": {"message": "no access SECRET"}}])})
    msg = check("google", "gemini-3.6-flash", "SECRET", c)
    assert "cannot use" in msg and "no access ***" in msg and "SECRET" not in msg


def test_unknown_model_on_probe_quota_and_bad_request():
    base = {("GET", "/models"): (404, {})}
    assert "does not know the model" in check("groq", "m", "k", client({**base, ("POST", "/chat/completions"): (404, {"error": {"message": "x"}})}))
    assert "quota" in check("groq", "m", "k", client({**base, ("POST", "/chat/completions"): (429, {"error": {"message": "x"}})}))
    assert "rejected the request" in check("groq", "m", "k", client({**base, ("POST", "/chat/completions"): (400, {"error": {"message": "x"}})}))


def test_anthropic_uses_its_own_endpoints_and_headers():
    seen = []

    def handler(req):
        seen.append((req.method, str(req.url), req.headers.get("x-api-key"), req.headers.get("authorization")))
        return httpx.Response(404 if req.method == "GET" else 200, json={})
    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert check("anthropic", "claude-x", "k", c) is None          # list unavailable -> probe
    assert seen[0][1].startswith("https://api.anthropic.com/v1/models") and seen[0][2] == "k" and seen[0][3] is None
    assert seen[1][0] == "POST" and seen[1][1] == "https://api.anthropic.com/v1/messages"


def test_a_listed_model_skips_the_probe_to_save_free_tier_quota():
    seen = []

    def handler(req):
        seen.append(req.method)
        return httpx.Response(200, json={"data": [{"id": "claude-x"}]})
    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert check("anthropic", "claude-x", "k", c) is None
    assert seen == ["GET"]


def test_network_failure_and_unknown_provider():
    def boom(req):
        raise httpx.ConnectError("down")
    c = httpx.Client(transport=httpx.MockTransport(boom))
    assert "Could not reach groq" in check("groq", "m", "k", c)
    assert "Unknown provider" in check("nope", "m", "k", c)


def test_google_style_400_api_key_not_valid_is_a_key_error():
    c = client({("GET", "/models"): (400, [{"error": {"message": "API key not valid. Please pass a valid API key."}}])})
    assert "rejected this API key" in check("google", "m", "k", c)


def _resolve(monkeypatch, mapping):
    """Make name lookups deterministic: host -> list of IPs."""
    import socket

    def fake(host, port, proto=0):
        if host not in mapping:
            raise socket.gaierror("no such host")
        return [(2, 1, 6, "", (ip, port)) for ip in mapping[host]]
    monkeypatch.setattr("codecheck.model_check.socket.getaddrinfo", fake)


def test_local_root_only_accepts_https_urls_on_public_addresses(monkeypatch):
    from codecheck.model_check import local_root
    _resolve(monkeypatch, {"abc.trycloudflare.com": ["104.16.1.1"], "lan.example.com": ["192.168.1.100"], "ts.example.com": ["100.118.115.34"],
                           "mixed.example.com": ["104.16.1.1", "10.0.0.5"], "lo.example.com": ["127.0.0.1"], "v6.example.com": ["fe80::1%en0"],
                           "meta.example.com": ["169.254.169.254"]})
    ok = "https://abc.trycloudflare.com"
    assert local_root(ok) == (ok, None)
    assert local_root(ok + "/v1/")[0] == ok and local_root(ok + "/v1/chat/completions")[0] == ok
    assert local_root("https://abc.trycloudflare.com:8443/ollama/v1")[0] == "https://abc.trycloudflare.com:8443/ollama"
    for bad in ("http://abc.trycloudflare.com", "https://user:pw@abc.trycloudflare.com", "ftp://abc.trycloudflare.com", "abc.trycloudflare.com", "https://"):
        assert local_root(bad)[0] is None, bad
    for private in ("lan.example.com", "ts.example.com", "mixed.example.com", "lo.example.com", "v6.example.com", "meta.example.com"):
        root, err = local_root(f"https://{private}")
        assert root is None and "private or local network" in err, private
    assert "Could not find" in local_root("https://nope.example.com")[1]
    assert local_root("https://localhost:11434")[0] is None and local_root("https://127.0.0.1")[0] is None


def test_detect_model_uses_the_one_model_the_server_has_or_has_loaded():
    from codecheck.model_check import detect_model
    root = "https://abc.trycloudflare.com"

    def models(*ids):
        return {("GET", "/v1/models"): (200, {"data": [{"id": i} for i in ids]})}
    assert detect_model(root, client=client(models("qwen2.5-coder:7b"))) == ("qwen2.5-coder:7b", None, [])
    # several models: Ollama's /api/ps says which one is loaded
    ollama = {**models("a:7b", "b:3b", "c:1b"), ("GET", "/api/ps"): (200, {"models": [{"name": "b:3b", "model": "b:3b"}]})}
    assert detect_model(root, client=client(ollama)) == ("b:3b", None, [])
    # LM Studio's /api/v0/models has a state (embedding models do not count)
    lm = {**models("x", "y", "emb"), ("GET", "/api/v0/models"): (200, {"data": [
        {"id": "x", "state": "not-loaded", "type": "llm"}, {"id": "y", "state": "loaded", "type": "llm"}, {"id": "emb", "state": "loaded", "type": "embeddings"}]})}
    assert detect_model(root, client=client(lm)) == ("y", None, [])


def test_several_loaded_models_become_a_list_to_choose_from_and_a_choice_is_checked():
    from codecheck.model_check import detect_model
    root = "https://abc.trycloudflare.com"
    two = {("GET", "/v1/models"): (200, {"data": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})}
    both_loaded = {**two, ("GET", "/api/ps"): (200, {"models": [{"name": "a"}, {"name": "b"}]})}
    assert detect_model(root, client=client(both_loaded)) == (None, None, ["a", "b"])           # offer the loaded ones
    assert detect_model(root, "b", client(both_loaded)) == ("b", None, [])                       # the choice is used
    assert detect_model(root, "zzz", client(both_loaded)) == (None, None, ["a", "b", "c"])       # a stale choice: offer again
    assert detect_model(root, client=client(two)) == (None, None, ["a", "b", "c"])               # cannot tell what is loaded: offer all


def test_detect_model_tells_the_user_what_to_do_on_their_machine():
    from codecheck.model_check import detect_model
    root = "https://abc.trycloudflare.com"
    two = {("GET", "/v1/models"): (200, {"data": [{"id": "a"}, {"id": "b"}]})}
    none_loaded = {**two, ("GET", "/api/ps"): (200, {"models": []})}
    msg = detect_model(root, client=client(none_loaded))[1]
    assert "No model is loaded" in msg and "Please load one" in msg and "a, b" in msg
    assert "has no models" in detect_model(root, client=client({("GET", "/v1/models"): (200, {"data": []})}))[1]
    assert "http-host-header" in detect_model(root, client=client({("GET", "/v1/models"): (403, {})}))[1]
    assert "nothing is answering" in detect_model(root, client=client({("GET", "/v1/models"): (502, {})}))[1]
    assert "HTTP 404" in detect_model(root, client=client({("GET", "/v1/models"): (404, {})}))[1]

    def boom(req):
        raise httpx.ConnectError("down")
    assert "Could not reach your model server" in detect_model(root, client=httpx.Client(transport=httpx.MockTransport(boom)))[1]


def test_malformed_model_response_shapes_do_not_raise_type_error():
    from codecheck.model_check import check, detect_model, list_models

    ids, err = list_models("google", "k", client({("GET", "/v1beta/openai/models"): (200, {"data": None})}))
    assert ids == [] and err is not None

    c = client({
        ("GET", "/v1beta/openai/models"): (200, {"data": None}),
        ("POST", "/v1beta/openai/chat/completions"): (200, {"choices": []}),
    })
    assert check("google", "gemini-3.6-flash", "k", c) is None

    root = "https://abc.trycloudflare.com"
    assert detect_model(root, client=client({("GET", "/v1/models"): (200, {"data": None})}))[1] is not None

    bad_ps = {
        ("GET", "/v1/models"): (200, {"data": [{"id": "a"}, {"id": "b"}]}),
        ("GET", "/api/ps"): (200, {"models": [{"name": ["unhashable"]}]}),
    }
    assert "No model is loaded" in detect_model(root, client=client(bad_ps))[1]

    bad_v0 = {
        ("GET", "/v1/models"): (200, {"data": [{"id": "a"}, {"id": "b"}]}),
        ("GET", "/api/ps"): (404, {}),
        ("GET", "/api/v0/models"): (200, {"data": None}),
    }
    assert detect_model(root, client=client(bad_v0)) == (None, None, ["a", "b"])
