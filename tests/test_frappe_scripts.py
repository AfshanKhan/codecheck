from pathlib import Path

import httpx
import pytest

from codecheck.frappe_scripts import FrappeScriptFetchError, fetch_via_api, write_scripts


def _mock_transport(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_client_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)


def test_write_scripts_routes_by_doctype_and_sanitizes_names(tmp_path: Path):
    scripts = [
        ("Server Script", "My Weird / Script:Name", "import os\n"),
        ("Client Script", "Some Client Script", "console.log('x')\n"),
    ]
    targets = write_scripts(scripts, tmp_path)

    assert [t.path for t in targets] == [
        "server_script/My_Weird_Script_Name.py",
        "client_script/Some_Client_Script.js",
    ]
    assert (tmp_path / "server_script/My_Weird_Script_Name.py").read_text() == "import os\n"
    assert (tmp_path / "client_script/Some_Client_Script.js").read_text() == "console.log('x')\n"
    assert all(t.status == "scanned" and t.changed_lines is None for t in targets)


def test_write_scripts_empty_or_symbol_only_name_falls_back_to_unnamed(tmp_path: Path):
    targets = write_scripts([("Server Script", "***", "pass\n")], tmp_path)
    assert targets[0].path == "server_script/unnamed.py"


def test_write_scripts_names_that_sanitize_to_the_same_path_dont_overwrite(tmp_path: Path):
    # regression (Greptile review): "a/b" and "a_b" both sanitize to
    # "a_b" -- the second write used to silently clobber the first on
    # disk, and both targets then pointed at the surviving content,
    # dropping one script from the audit entirely.
    scripts = [
        ("Server Script", "a/b", "first\n"),
        ("Server Script", "a_b", "second\n"),
    ]
    targets = write_scripts(scripts, tmp_path)

    assert [t.path for t in targets] == ["server_script/a_b.py", "server_script/a_b-2.py"]
    assert (tmp_path / "server_script/a_b.py").read_text() == "first\n"
    assert (tmp_path / "server_script/a_b-2.py").read_text() == "second\n"


def test_fetch_via_api_sends_auth_header_and_parses_both_doctypes(monkeypatch):
    captured_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        if "Server%20Script" in str(request.url):
            return httpx.Response(200, json={"data": [{"name": "srv1", "script": "import os\n"}]})
        return httpx.Response(200, json={"data": [{"name": "cli1", "script": "console.log(1)\n"}]})

    _mock_transport(monkeypatch, handler)

    results = fetch_via_api("https://example.com", "mykey", "mysecret")

    assert results == [
        ("Server Script", "srv1", "import os\n"),
        ("Client Script", "cli1", "console.log(1)\n"),
    ]
    assert all(r.headers["Authorization"] == "token mykey:mysecret" for r in captured_requests)


def test_fetch_via_api_skips_empty_scripts(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"name": "a", "script": "x = 1\n"}, {"name": "b", "script": ""}]},
        )

    _mock_transport(monkeypatch, handler)

    results = fetch_via_api("https://example.com", "k", "s")
    assert len(results) == 2  # one per doctype, "b" (empty script) dropped from each
    assert all(name == "a" for _doctype, name, _script in results)


def test_fetch_via_api_raises_on_http_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"exc": "not authorized"})

    _mock_transport(monkeypatch, handler)

    with pytest.raises(httpx.HTTPStatusError):
        fetch_via_api("https://example.com", "k", "s")


def test_fetch_via_api_rejects_plain_http_for_a_remote_host():
    # regression (Greptile review, security): an http:// site URL would
    # send the API key/secret in plaintext over the network.
    with pytest.raises(FrappeScriptFetchError, match="plain HTTP"):
        fetch_via_api("http://example.com", "k", "s")


def test_fetch_via_api_allows_plain_http_for_localhost(monkeypatch):
    # http://localhost is a real local dev setup, not a network-exposed
    # credential leak -- should reach the (mocked) request, not be rejected
    # for its scheme.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    _mock_transport(monkeypatch, handler)
    assert fetch_via_api("http://127.0.0.1:8000", "k", "s") == []


def test_fetch_via_api_raises_on_invalid_json(monkeypatch):
    # regression (Greptile review): invalid JSON used to raise an uncaught
    # json.JSONDecodeError instead of the command's normal fetch error.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    _mock_transport(monkeypatch, handler)
    with pytest.raises(FrappeScriptFetchError, match="wasn't valid JSON"):
        fetch_via_api("https://example.com", "k", "s")


def test_fetch_via_api_raises_when_data_field_is_missing_or_wrong_shape(monkeypatch):
    # regression (Greptile review): a response with no "data" field used
    # to be silently treated as "zero scripts found" via .get("data", []).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    _mock_transport(monkeypatch, handler)
    with pytest.raises(FrappeScriptFetchError, match="unexpected response shape"):
        fetch_via_api("https://example.com", "k", "s")


def test_fetch_via_api_skips_rows_missing_name_instead_of_crashing(monkeypatch):
    # regression (Greptile review): a row with "script" but no "name" used
    # to raise an uncaught KeyError on row["name"].
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"script": "x = 1\n"}]})

    _mock_transport(monkeypatch, handler)
    assert fetch_via_api("https://example.com", "k", "s") == []
