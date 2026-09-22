from pathlib import Path

import httpx
import pytest

from codecheck.frappe_scripts import fetch_via_api, write_scripts


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


def test_fetch_via_api_sends_auth_header_and_parses_both_doctypes(monkeypatch):
    captured_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        if "Server%20Script" in str(request.url):
            return httpx.Response(200, json={"data": [{"name": "srv1", "script": "import os\n"}]})
        return httpx.Response(200, json={"data": [{"name": "cli1", "script": "console.log(1)\n"}]})

    transport = httpx.MockTransport(handler)
    real_client_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

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

    transport = httpx.MockTransport(handler)
    real_client_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    results = fetch_via_api("https://example.com", "k", "s")
    assert len(results) == 2  # one per doctype, "b" (empty script) dropped from each
    assert all(name == "a" for _doctype, name, _script in results)


def test_fetch_via_api_raises_on_http_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"exc": "not authorized"})

    transport = httpx.MockTransport(handler)
    real_client_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    with pytest.raises(httpx.HTTPStatusError):
        fetch_via_api("https://example.com", "k", "s")
