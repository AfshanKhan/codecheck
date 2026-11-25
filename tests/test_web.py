import json
import time

import pytest

flask = pytest.importorskip("flask")
from codecheck import web  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path)
    monkeypatch.setattr(web.jobs, "put", lambda item: None)
    return web.app.test_client()


@pytest.mark.parametrize("url", [
    "/etc/passwd", "file:///etc/passwd", "http://github.com/o/r", "https://evil.com/o/r",
    "https://github.com.evil.com/o/r", "https://github.com/o", "https://github.com/o/r/issues/1",
    "https://github.com/o/r?x=1", "-oProxyCommand=x",
])
def test_rejects_non_github_targets(client, url):
    assert client.post("/run", data={"target": url}).status_code == 400


@pytest.mark.parametrize("url,kind", [
    ("https://github.com/o/r", "repo"), ("https://github.com/o/r/", "repo"),
    ("https://github.com/o/r.git", "repo"), ("https://github.com/o/r/pull/12", "pr"),
])
def test_accepts_github_targets(client, tmp_path, url, kind):
    r = client.post("/run", data={"target": url})
    assert r.status_code == 302
    run = json.loads(next(tmp_path.glob("*/run.json")).read_text())
    assert run["kind"] == kind and run["status"] == "queued"


def test_key_only_in_env_never_argv_or_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path)
    (tmp_path / "abc").mkdir()
    spec = {"kind": "pr", "target": "https://github.com/o/r/pull/1", "cloud": True,
            "provider": "groq", "model": "m", "key": "SECRET-KEY"}
    argv, env = web.build("abc", spec)
    assert "SECRET-KEY" not in " ".join(argv)
    assert env["CODECHECK_WEB_KEY"] == "SECRET-KEY"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["HOME"].startswith(str(tmp_path))
    assert "SECRET-KEY" not in (tmp_path / "abc" / "config.yaml").read_text()
    assert '"eslint": false' in (tmp_path / "abc" / "config.yaml").read_text()


def test_download_only_listed_files_and_valid_ids(client, tmp_path):
    (tmp_path / "abcdef012345" / "out").mkdir(parents=True)
    (tmp_path / "abcdef012345" / "out" / "a.md").write_text("hi")
    (tmp_path / "secret.txt").write_text("nope")
    (tmp_path / "abcdef012345" / "run.json").write_text(json.dumps(
        {"id": "abcdef012345", "created": time.time(), "status": "done", "target": "https://github.com/o/r",
         "mode": "Repo audit", "cloud": False, "files": ["a.md"], "log": ""}))
    assert client.get("/r/abcdef012345/f/a.md").data == b"hi"
    assert client.get("/r/abcdef012345/f/../../secret.txt").status_code == 404
    assert client.get("/r/abcdef012345/f/other.md").status_code == 404
    assert client.get("/r/..%2f..%2fetc/f/x").status_code == 404


def test_token_file_is_used_for_any_repo_and_never_stored(client, tmp_path, monkeypatch):
    sec = tmp_path / "sec"
    sec.mkdir()
    (sec / "github-token").write_text("TOK\n")
    monkeypatch.setattr(web, "SECRETS", sec)
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(web, "RUNS", runs)
    sent = []
    monkeypatch.setattr(web.jobs, "put", sent.append)
    assert client.post("/run", data={"target": "https://github.com/Any/Repo"}).status_code == 302
    assert sent[0][1]["token"] == "TOK"
    assert "TOK" not in next(runs.glob("*/run.json")).read_text()
    assert b"any/repo" not in client.get("/").data.lower()               # nothing is listed


def test_token_goes_to_git_via_env_only(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path)
    (tmp_path / "abc").mkdir()
    spec = {"kind": "repo", "target": "https://github.com/o/r", "cloud": False, "provider": "", "model": "", "token": "TOK"}
    argv, env = web.build("abc", spec)
    assert "TOK" not in " ".join(argv) and "TOK" not in (tmp_path / "abc" / "config.yaml").read_text()
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ")


def test_pr_base_ref_is_passed_when_known(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path)
    (tmp_path / "abc").mkdir()
    monkeypatch.setattr(web, "pr_base_ref", lambda target, token: "develop")
    spec = {"kind": "pr", "target": "https://github.com/o/r/pull/1", "cloud": False, "provider": "", "model": ""}
    argv, _ = web.build("abc", spec)
    assert argv[argv.index("--base-ref") + 1] == "develop"
    monkeypatch.setattr(web, "pr_base_ref", lambda target, token: None)
    assert "--base-ref" not in web.build("abc", spec)[0]


def _steps(r, log, pos=1):
    return [(s["state"], s["detail"]) for s in web.progress(r, log, pos)["steps"]]


def test_public_log_hides_server_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    raw = (f"Cloning into '/tmp/codecheck-clone-ab12cd'\nError at {tmp_path}/runs/abc/home/x and {web.Path.home()}/codecheck/.venv/y\n"
           f"Reports written to {tmp_path}/runs/abc/out/a.md")
    out = web.public_log(raw)
    assert "/tmp/codecheck-clone" not in out and str(tmp_path) not in out and str(web.Path.home()) not in out
    assert "Reports written" not in out and "<repo>" in out


def test_report_page_renders_findings_and_escapes_untrusted_text(client, tmp_path):
    rid = "abcdef012345"
    out = tmp_path / rid / "out"
    out.mkdir(parents=True)
    (out / "r.json").write_text(json.dumps({
        "generated_at": "2026-11-06T10:40:21+00:00", "tiers_run": ["rules", "cloud_llm"], "duration_seconds": 4.2,
        "file_hashes": {"a.py": "x"}, "skipped": ["cloud_llm: b.js: no tool call"], "compliance_percentage": 82.1,
        "counts_by_severity": {"low": 1, "high": 1},
        "findings": [
            {"check_id": "RUFF-W291", "source": "ruff", "severity": "low", "title": "Trailing whitespace",
             "explanation": "Trailing whitespace", "file": "a.py", "line_start": 3, "line_end": 3, "suggestion": None},
            {"check_id": "RULE-1", "source": "house", "severity": "high", "title": "<script>alert(1)</script>",
             "explanation": "bad <b>x</b>", "file": "a.py", "line_start": 1, "line_end": 2, "suggestion": "fix()"}]}))
    (tmp_path / rid / "run.json").write_text(json.dumps(
        {"id": rid, "created": time.time(), "status": "done", "target": "https://github.com/o/r", "mode": "Repo audit",
         "cloud": True, "provider": "groq", "files": ["r.json"], "log": ""}))
    html = client.get(f"/r/{rid}").data.decode()
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html and "<b>x</b>" not in html
    assert "82%" in html and "Trailing whitespace" in html and "line 1&ndash;2" in html and "Not checked" in html
    assert html.index("badge high") < html.index("badge low")      # worst first


def test_progress_steps_follow_the_cli_output():
    r = {"status": "running", "cloud": True, "created": 0, "started": 1}
    def st(log, **kw):
        return [(s["name"][:8], s["state"], s["detail"]) for s in web.progress({**r, **kw}, log, 1)["steps"]]
    assert [s[1] for s in st("")] == ["done", "active", "todo", "todo", "todo", "todo"]            # checking key + model
    assert [s[1] for s in st("API key and model OK\n")] == ["done", "done", "active", "todo", "todo", "todo"]
    assert [s[1] for s in st("API key and model OK\nRunning rules (ruff...")] == ["done", "done", "done", "active", "todo", "todo"]
    ai = "API key and model OK\nRunning rules\nAI review: 7 file(s)\n  (3/7) a.py: 0 findings\n"
    assert st(ai)[4] == ("AI revie", "active", "3 of 7 files")
    assert st(ai + "Reports written to x")[-1][1] == "active"
    assert {s[1] for s in st(ai, status="done")} == {"done"}
    # key/model rejected: no AI step, the rules still run
    skipped = "AI review skipped: bad key\nRunning rules\n"
    steps = st(skipped, ai_error="bad key")
    assert len(steps) == 5 and "AI revie" not in [s[0] for s in steps] and steps[3][1] == "active"
    assert len(web.progress({**r, "cloud": False}, "", 1)["steps"]) == 4


def test_preflight_explains_each_reason_the_ai_cannot_run(monkeypatch):
    monkeypatch.setattr(web, "check_model", lambda provider, model, key: "google has no model called x.")
    ok = {"provider": "google", "model": "gemini-3.6-flash", "key": "k"}
    assert "not one of the supported" in web.preflight({**ok, "provider": "evil"})
    assert "No API key" in web.preflight({**ok, "key": ""})
    assert "not a valid model ID" in web.preflight({**ok, "model": "Gemini 3.5 Flash"})
    assert web.preflight(ok) == "google has no model called x."
    monkeypatch.setattr(web, "check_model", lambda provider, model, key: None)
    assert web.preflight(ok) is None


def test_bad_key_or_model_never_blocks_the_run_and_the_reason_goes_to_the_report(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path)
    monkeypatch.setattr(web, "check_model", lambda provider, model, key: "google rejected this API key (HTTP 400).")
    sent = []
    monkeypatch.setattr(web.jobs, "put", sent.append)
    r = client.post("/run", data={"target": "https://github.com/o/r", "cloud": "1", "provider": "google", "model": "nope", "key": "K"})
    assert r.status_code == 302 and len(sent) == 1                      # accepted despite the bad key/model
    rid, spec = sent[0]
    captured = {}
    monkeypatch.setattr(web.subprocess, "Popen", lambda argv, **kw: (_ for _ in ()).throw(OSError("stop here")) if captured.update(argv=argv) is None else None)
    web.execute(rid, spec)
    argv = captured["argv"]
    assert "--cloud" not in argv                                          # rules only
    assert argv[argv.index("--skip-note") + 1] == "AI review did not run: google rejected this API key (HTTP 400)."
    run = json.loads((tmp_path / rid / "run.json").read_text())
    assert run["ai_error"] == "google rejected this API key (HTTP 400)."
    html = client.get(f"/r/{rid}").data.decode()
    assert "AI review did not run." in html


def test_no_file_cap_and_no_time_limit_in_the_run_config(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path)
    (tmp_path / "abc").mkdir()
    web.build("abc", {"kind": "repo", "target": "https://github.com/o/r", "cloud": True, "provider": "groq", "model": "m", "key": "k"})
    cfg = json.loads((tmp_path / "abc" / "config.yaml").read_text())["cloud"]
    assert cfg["audit_file_cap"] >= 10**6 and "time_budget_seconds" not in cfg


def test_main_starts_the_configured_number_of_workers(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path)
    monkeypatch.setattr(web, "WORKERS", 2)
    started = []

    class FakeThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            started.append(self.target)
    monkeypatch.setattr(web.threading, "Thread", FakeThread)
    monkeypatch.setattr("waitress.serve", lambda *a, **kw: None)
    web.main()
    assert started == [web.worker, web.worker]


def test_two_runs_can_execute_at_once(tmp_path, monkeypatch):
    """Two workers each take a job from the shared queue; neither waits for the other."""
    import threading as t
    monkeypatch.setattr(web, "RUNS", tmp_path)
    both_running = t.Barrier(2, timeout=5)
    done = []

    def fake_execute(rid, spec):
        both_running.wait()                      # only passes if the other worker is inside execute() too
        done.append(rid)
    monkeypatch.setattr(web, "execute", fake_execute)
    q = web.queue.Queue()
    monkeypatch.setattr(web, "jobs", q)
    monkeypatch.setattr(web, "prune", lambda: None)
    for rid in ("a", "b"):
        (tmp_path / rid).mkdir()
        q.put((rid, {}))
    workers = [t.Thread(target=web.worker, daemon=True) for _ in range(2)]
    for w in workers:
        w.start()
    for _ in range(50):
        if len(done) == 2:
            break
        t.Event().wait(0.1)
    assert sorted(done) == ["a", "b"]


def _run(status, kind="pr", cloud=False, created=0.0, started=None, finished=None):
    return {"id": "x", "status": status, "kind": kind, "cloud": cloud, "created": created, "started": started, "finished": finished}


def test_wait_estimate_uses_recent_run_times_and_both_lanes(monkeypatch):
    monkeypatch.setattr(web, "WORKERS", 2)
    now = web.time.time()
    done = [_run("done", started=now - 1000, finished=now - 940)]                  # PR rules-only reviews take 60 s
    one = done + [_run("running", started=now - 10)]
    assert web.wait_seconds([], _run("queued"), one) == 0                          # a lane is free
    two = done + [_run("running", started=now - 10), _run("running", started=now - 10)]
    assert 45 <= web.wait_seconds([], _run("queued"), two) <= 50                   # next lane frees in about 50 s
    assert 105 <= web.wait_seconds([_run("queued"), _run("queued")], _run("queued"), two) <= 115   # two ahead: each lane takes one more
    assert web.wait_seconds([], _run("queued"), []) == 0                           # nothing running yet


def test_wait_text():
    assert web.human(10) == "less than a minute" and web.human(60) == "about a minute"
    assert web.human(300) == "about 5 minutes" and web.human(7200) == "over an hour"


def test_form_and_queue_notice_only_when_every_lane_is_busy(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "WORKERS", 2)
    monkeypatch.setattr(web, "RUNS", tmp_path)

    def put(rid, status, created, **kw):
        (tmp_path / rid).mkdir(exist_ok=True)
        (tmp_path / rid / "run.json").write_text(json.dumps({**_run(status, created=created, started=time.time() - 5), "id": rid, "target": "https://github.com/o/r",
                                                           "mode": "PR review", "files": [], "log": "", **kw}))
    put("aaaaaaaaaaaa", "running", 1)
    assert b"review lanes are busy" not in client.get("/").data                     # one lane is still free
    put("bbbbbbbbbbbb", "running", 2)
    page = client.get("/").data.decode()
    assert "All 2 review lanes are busy" in page and "before it starts" in page
    put("cccccccccccc", "queued", 3)
    d = client.get("/r/cccccccccccc/status").get_json()
    assert d["queue"]["pos"] == 1 and d["queue"]["busy"] is True and "minute" in d["queue"]["text"]
    assert client.get("/r/aaaaaaaaaaaa/status").get_json()["queue"] is None


def test_branch_field_is_validated_stored_for_repos_only_and_passed_to_the_cli(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path)
    sent = []
    monkeypatch.setattr(web.jobs, "put", sent.append)
    for bad in ("-oProxyCommand=x", "a..b", "a b", "x;rm", "/abs"):
        assert client.post("/run", data={"target": "https://github.com/o/r", "branch": bad}).status_code == 400
    assert client.post("/run", data={"target": "https://github.com/o/r", "branch": "feat/x-1.2"}).status_code == 302
    assert sent[0][1]["branch"] == "feat/x-1.2"
    assert client.post("/run", data={"target": "https://github.com/o/r/pull/3", "branch": "develop"}).status_code == 302
    assert sent[1][1]["branch"] == ""                                       # ignored for a PR
    (tmp_path / "abc").mkdir()
    argv, _ = web.build("abc", {"kind": "repo", "target": "https://github.com/o/r", "branch": "develop", "cloud": False, "provider": "", "model": ""})
    assert argv[argv.index("--branch") + 1] == "develop"
    assert "--branch" not in web.build("abc", {"kind": "repo", "target": "https://github.com/o/r", "branch": "", "cloud": False, "provider": "", "model": ""})[0]


def test_missing_branch_error_is_explained():
    out = web.public_log("Error: Could not clone 'https://github.com/o/r':\nfatal: Remote branch nope not found in upstream origin")
    assert 'branch "nope" does not exist' in out and "upstream" not in out


def test_local_model_option_detects_the_model_then_builds_the_run(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path)
    sent = []
    monkeypatch.setattr(web.jobs, "put", sent.append)
    page = client.get("/").data.decode()
    assert "my own local model" in page and "cloudflared tunnel" in page and 'name=local_model' not in page   # nothing to type or pick yet
    monkeypatch.setattr(web, "local_root", lambda url: ("https://x.trycloudflare.com", None))
    monkeypatch.setattr(web, "detect_model", lambda root, chosen=None: (chosen or "q:7b", None, []))     # one model: used, no questions
    assert client.post("/run", data={"target": "https://github.com/o/r", "cloud": "2", "local_url": "https://x.trycloudflare.com",
                                     "key": "ignored"}).status_code == 302
    rid, spec = sent[0]
    assert spec["local"] and spec["cloud"] and spec["provider"] == "local" and spec["key"] == ""
    run = json.loads((tmp_path / rid / "run.json").read_text())
    assert run["local"] is True and run["provider"] == "local" and "trycloudflare" not in json.dumps(run)   # the tunnel URL is never stored
    # preflight: bad address -> reason; then the model is detected from the server
    monkeypatch.setattr(web, "local_root", lambda url: (None, "That address points to a private or local network."))
    assert "private or local network" in web.preflight(dict(spec))
    monkeypatch.setattr(web, "local_root", lambda url: ("https://x.trycloudflare.com", None))
    monkeypatch.setattr(web, "detect_model", lambda root, chosen=None: (None, "No model is loaded on your server. Please load one.", []))
    assert "Please load one" in web.preflight(dict(spec))
    monkeypatch.setattr(web, "detect_model", lambda root, chosen=None: (None, None, ["a", "b"]))
    assert "More than one model is loaded" in web.preflight(dict(spec))
    monkeypatch.setattr(web, "detect_model", lambda root, chosen=None: (chosen or "qwen2.5-coder:7b", None, []))
    s2 = dict(spec)
    assert web.preflight(s2) is None and s2["local_root"] == "https://x.trycloudflare.com" and s2["model"] == "qwen2.5-coder:7b"
    # build: local tier configured with the detected model, cloud off, key never involved
    (tmp_path / "abc").mkdir()
    argv, env = web.build("abc", s2)
    cfg = json.loads((tmp_path / "abc" / "config.yaml").read_text())
    assert cfg["cloud"]["enabled"] is False and cfg["local"]["enabled"] is True and cfg["local"]["provider"] == "openai_compatible"
    assert cfg["local"]["model"] == "qwen2.5-coder:7b" and cfg["local"]["skip_noise"] is True
    assert cfg["local"]["base_url"] == "https://x.trycloudflare.com/v1/chat/completions"
    assert "--local" in argv and "--force-local" in argv and "--cloud" not in argv and "CODECHECK_WEB_KEY" not in env
    assert web.scrub("fail at https://x.trycloudflare.com/v1/chat", s2) == "fail at ***/v1/chat"


def test_local_model_that_cannot_be_reached_still_gives_a_rules_report(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path)
    sent = []
    monkeypatch.setattr(web.jobs, "put", sent.append)
    client.post("/run", data={"target": "https://github.com/o/r", "cloud": "2", "local_url": "http://localhost:11434"})
    rid, spec = sent[0]
    captured = {}
    monkeypatch.setattr(web.subprocess, "Popen", lambda argv, **kw: (_ for _ in ()).throw(OSError("stop")) if captured.update(argv=argv) is None else None)
    web.execute(rid, spec)
    argv = captured["argv"]
    assert "--local" not in argv and "--cloud" not in argv                      # rules only
    note = argv[argv.index("--skip-note") + 1]
    assert note.startswith("AI review did not run: Use the https:// address")
    run = json.loads((tmp_path / rid / "run.json").read_text())
    assert run["ai_error"].startswith("Use the https:// address")
    steps = [s["name"] for s in web.progress(run | {"cloud": True, "status": "running"}, "Checking the connection and model...\n", 1)["steps"]]
    assert "Checking the connection to your model" in steps


def test_local_model_setup_steps_are_on_the_page(client):
    page = client.get("/").data.decode()
    for needle in ("How to set this up", "ollama pull", "ollama ps", "ollama run", "the page asks which one to use", "no model name to type", "OLLAMA_CONTEXT_LENGTH=16384", "brew install cloudflared",
                   "cloudflared tunnel --url http://localhost:11434 --http-host-header=localhost:11434",
                   "cloudflared tunnel --url http://localhost:1234", "trycloudflare.com", "100 seconds"):
        assert needle in page, needle


def test_several_loaded_models_show_a_dropdown_before_the_review_is_queued(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path)
    sent = []
    monkeypatch.setattr(web.jobs, "put", sent.append)
    monkeypatch.setattr(web, "local_root", lambda url: ("https://x.trycloudflare.com", None))
    loaded = ["a:7b", "b:3b"]
    monkeypatch.setattr(web, "detect_model", lambda root, chosen=None: (chosen, None, []) if chosen in loaded else (None, None, loaded))
    form = {"target": "https://github.com/o/r", "cloud": "2", "local_url": "https://x.trycloudflare.com"}
    r = client.post("/run", data=form)
    html = r.data.decode()
    assert r.status_code == 200 and not sent                                  # nothing queued yet
    assert "more than one model loaded" in html and '<select id=local_model name=local_model>' in html
    assert "<option >a:7b</option>" in html and "<option >b:3b</option>" in html and 'value="https://x.trycloudflare.com"' in html
    r = client.post("/run", data={**form, "local_model": "b:3b"})
    assert r.status_code == 302 and sent[0][1]["model"] == "b:3b"             # the choice is used
    sent.clear()
    stale = client.post("/run", data={**form, "local_model": "gone:1b"})
    assert stale.status_code == 200 and not sent and b"not on your server" in stale.data
    # nothing loaded or unreachable: still queued, so the rules report gets made and says why the AI did not run
    monkeypatch.setattr(web, "detect_model", lambda root, chosen=None: (None, "No model is loaded on your server. Please load one.", []))
    assert client.post("/run", data=form).status_code == 302 and len(sent) == 1


def test_model_dropdown_and_effort_reach_the_run_config(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path)
    page = client.get("/").get_data(as_text=True)
    assert 'id=model name=model></select>' in page and "<option selected>medium</option>" in page      # effort defaults to medium
    assert "claude-sonnet-5-5" in page and "gemini-3.6-flash" in page                                    # catalog is on the page
    sent = []
    monkeypatch.setattr(web.jobs, "put", sent.append)
    form = {"target": "https://github.com/o/r", "cloud": "1", "provider": "google", "key": "K"}
    client.post("/run", data={**form, "model": "gemini-3.5-flash-lite", "effort": "high"})
    assert sent[-1][1]["model"] == "gemini-3.5-flash-lite" and sent[-1][1]["effort"] == "high"
    client.post("/run", data=form)                                                                        # nothing picked: provider default, medium
    assert sent[-1][1]["model"] == "gemini-3.6-flash" and sent[-1][1]["effort"] == "medium"
    client.post("/run", data={**form, "provider": "mistral", "effort": "high"})                          # model has no effort setting
    assert sent[-1][1]["effort"] is None
    client.post("/run", data={**form, "model": "bad model!", "effort": "turbo"})                          # invalid values fall back
    assert sent[-1][1]["model"] == "gemini-3.6-flash" and sent[-1][1]["effort"] == "medium"
    client.post("/run", data={**form, "model": "gemini-2.5-pro"})                                         # a model from the key's own list
    assert sent[-1][1]["model"] == "gemini-2.5-pro" and sent[-1][1]["effort"] == "medium"
    client.post("/run", data={**form, "provider": "groq", "model": "gemma2-9b-it"})
    assert sent[-1][1]["effort"] is None                                                                  # not known to take effort


def test_effort_is_sent_in_each_provider_s_own_shape(monkeypatch):
    import httpx

    from codecheck.config import CloudConfig
    from codecheck.models import ReviewTarget
    from codecheck.reviewers import cloud_llm, openai_protocol
    from codecheck.reviewers.cloud_llm import build_cloud_reviewer
    seen = []

    def fake_post(client, url, payload, *a, **k):
        seen.append(payload)
        raise httpx.ConnectError("stop here")           # the payload is all this test needs
    monkeypatch.setattr(cloud_llm, "post_with_retry", fake_post)
    monkeypatch.setattr(openai_protocol, "post_with_retry", fake_post)
    t = ReviewTarget(path="a.py", status="scanned")
    for provider, effort, want in (("anthropic", "medium", {"output_config": {"effort": "medium"}}),
                                   ("google", "high", {"reasoning_effort": "high"}),
                                   ("openrouter", "low", {"reasoning": {"effort": "low"}}),
                                   ("google", None, {})):
        rv = build_cloud_reviewer(CloudConfig(enabled=True, provider=provider, model="m", effort=effort))
        rv._review_file(httpx.Client(), t, "x=1")
        assert {k: v for k, v in seen[-1].items() if k in ("output_config", "reasoning_effort", "reasoning")} == want, provider


def test_anthropic_falls_back_to_auto_tool_choice_when_forcing_is_rejected(monkeypatch):
    import httpx

    from codecheck.config import CloudConfig
    from codecheck.models import ReviewTarget
    from codecheck.reviewers.cloud_llm import build_cloud_reviewer
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body["tool_choice"]["type"])
        if body["tool_choice"]["type"] == "tool":
            return httpx.Response(400, json={"error": {"message": 'tool_choice: type "tool" and "any" are not supported for this model.'}})
        return httpx.Response(200, json={"content": [{"type": "tool_use", "name": "report_findings", "input": {"findings": []}}]})
    rv = build_cloud_reviewer(CloudConfig(enabled=True, provider="anthropic", model="claude-sonnet-5-5"))
    findings, err = rv._review_file(httpx.Client(transport=httpx.MockTransport(handler)), ReviewTarget(path="a.py", status="scanned"), "x=1")
    assert (findings, err) == ([], None) and calls == ["tool", "auto"]


def test_models_endpoint_lists_what_the_key_can_use(client, monkeypatch):
    monkeypatch.setattr(web, "list_models", lambda provider, key: (["gemini-2.5-pro", "gemini-3.6-flash"], None))
    r = client.post("/models", data={"provider": "google", "key": "K"})
    assert r.get_json() == {"models": ["gemini-2.5-pro", "gemini-3.6-flash"], "error": None} and r.headers["Cache-Control"] == "no-store"
    assert client.post("/models", data={"provider": "google", "key": ""}).status_code == 400
    assert client.post("/models", data={"provider": "evil", "key": "K"}).status_code == 400
    assert "Load every model my key can use" in client.get("/").get_data(as_text=True)


def test_list_models_reads_the_provider_list_and_drops_non_chat_models():
    import httpx

    from codecheck import model_check
    body = {"data": [{"id": "models/gemini-3.6-flash"}, {"id": "models/text-embedding-004"}, {"id": "models/gemini-2.5-pro"}, {"id": "models/imagen-4"}]}

    def handler(request):
        return httpx.Response(200 if request.headers["authorization"] == "Bearer good" else 400,
                              json=body if request.headers["authorization"] == "Bearer good" else {"error": {"message": "API key not valid"}})
    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert model_check.list_models("google", "good", c) == (["gemini-2.5-pro", "gemini-3.6-flash"], None)
    ids, err = model_check.list_models("google", "bad", c)
    assert ids == [] and "rejected" in err
    assert model_check.list_models("nope", "k", c)[1].startswith("Unknown provider")


def test_the_model_script_comes_after_every_element_it_uses(client):
    page = client.get("/").get_data(as_text=True)
    script = page.index("<script>(function(){var M=")
    for el in ('id=provider', 'id=model', 'id=key', 'id=loadm', 'id=loadnote'):
        assert page.index(el) < script, el          # a script placed earlier finds null and the button does nothing


def test_list_models_keeps_text_models_only():
    import httpx

    from codecheck import model_check
    google = {"data": [{"id": "models/gemini-3.6-flash"}, {"id": "models/nano-banana-pro-preview"}, {"id": "models/gemini-2.5-flash-image"},
                       {"id": "models/gemini-3-pro-image-preview"}, {"id": "models/veo-3.1"}, {"id": "models/lyria-3"},
                       {"id": "models/gemma-3-27b-it"}, {"id": "models/gemini-2.5-flash-native-audio-live"}, {"id": "models/gemini-embedding-001"}]}
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=google)))
    assert model_check.list_models("google", "k", c)[0] == ["gemini-3.6-flash", "gemma-3-27b-it"]
    router = {"data": [{"id": "a/chat", "architecture": {"output_modalities": ["text"]}},
                       {"id": "b/picture", "architecture": {"output_modalities": ["image", "text"]}},
                       {"id": "c/pic-only", "architecture": {"output_modalities": ["image"]}},
                       {"id": "d/unlabelled"}]}
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=router)))
    assert model_check.list_models("openrouter", "k", c)[0] == ["a/chat", "d/unlabelled"]
