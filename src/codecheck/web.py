"""Small web UI for trying codecheck: paste a public GitHub PR or repo URL, optionally an LLM
API key, and read the report. Shells out to the CLI, one job at a time.

Meant for a short trial with a few trusted people. There is NO login, so anyone with the page link can
submit a review. Reviews are not listed anywhere; a report opens only from its unguessable link, and
all runs stay on disk for the owner to check later. What it does to limit the damage:
- only https://github.com URLs (no local paths, no other hosts); if ~/codecheck-secrets/github-token
  exists, that read-only token is used for every clone, so private repos it can read work too
- each run gets an empty HOME and no git config, so the server's stored git tokens are never used
- ESLint is off (it would execute code from the reviewed repo)
- the API key goes to the child process through the environment only: never in argv, on disk or
  in the run log; the provider list is fixed so the key can only go to a known API host
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    abort,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    url_for,
)

from codecheck.model_check import check as check_model
from codecheck.model_check import detect_model, list_models, local_root

RUNS = Path(os.environ.get("CODECHECK_WEB_RUNS", Path.home() / "codecheck-web-runs")).resolve()
STALL = 60 * 60         # kill a run only if it prints nothing for this long (a hung process would block the queue)
KEEP = 100              # newest runs kept on disk
MAX_QUEUE = 5
WORKERS = int(os.environ.get("CODECHECK_WEB_WORKERS", "2"))  # reviews that run at the same time
CODE_EXTENSIONS = [".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".vue", ".php", ".rb", ".go", ".java", ".cs", ".rs", ".c", ".cpp", ".sh", ".sql"]
PROVIDERS = {           # provider -> default model (the CLI knows each provider's API host)
    "anthropic": "claude-sonnet-5-5",
    "groq": "llama-3.3-70b-versatile",
    "mistral": "mistral-large-latest",
    "cerebras": "llama-3.3-70b",
    "openrouter": "anthropic/claude-sonnet-5.5",
    "google": "gemini-3.6-flash",
}
MODELS = {              # provider -> model IDs offered in the page (first = default); True = takes a reasoning-effort setting
    "anthropic": {"claude-sonnet-5-5": True, "claude-opus-5-5": True, "claude-fable-5-1": True, "claude-haiku-5-5": True},
    "google": {"gemini-3.6-flash": True, "gemini-3.5-flash-lite": True},
    "groq": {"llama-3.3-70b-versatile": False, "openai/gpt-oss-120b": True, "openai/gpt-oss-20b": True},
    "mistral": {"mistral-large-latest": False, "mistral-medium-latest": False, "mistral-small-latest": False},
    "cerebras": {"llama-3.3-70b": False, "gpt-oss-120b": True},
    "openrouter": {"anthropic/claude-sonnet-5.5": True, "openai/gpt-oss-120b": True},
}
assert all(PROVIDERS[p] in m for p, m in MODELS.items())
EFFORTS = ("low", "medium", "high")
_EFFORT_MODEL = re.compile(r"gpt-oss|gemini-(2\.5|[3-9])|claude", re.I)   # models from the provider's list that take an effort setting


def takes_effort(provider: str, model: str) -> bool:
    known = MODELS.get(provider, {})
    return known[model] if model in known else bool(_EFFORT_MODEL.search(model))
_NAME = r"[A-Za-z0-9_.-]+"
PR_RE = re.compile(rf"^https://github\.com/{_NAME}/{_NAME}/pull/\d+/?$")
REPO_RE = re.compile(rf"^https://github\.com/{_NAME}/{_NAME}/?$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")
ID_RE = re.compile(r"^[0-9a-f]{12}$")
SLUG_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/pull/\d+)?/?$")
# Optional read-only GitHub token in SECRETS/github-token, used for every clone.
SECRETS = Path(os.environ.get("CODECHECK_WEB_SECRETS", Path.home() / "codecheck-secrets"))

app = Flask(__name__)
jobs: queue.Queue = queue.Queue()
lock = threading.Lock()
submit_lock = threading.Lock()


def run_dir(rid: str) -> Path:
    if not ID_RE.match(rid):
        abort(404)
    return RUNS / rid


def load(rid: str) -> dict:
    try:
        return json.loads((run_dir(rid) / "run.json").read_text())
    except (OSError, ValueError):
        abort(404)


def save(rid: str, **fields) -> None:
    with lock:
        p = RUNS / rid / "run.json"
        data = json.loads(p.read_text()) if p.exists() else {}
        data.update(fields)
        tmp = p.with_suffix(".tmp")   # write aside, then swap: a reader never sees a half-written file
        tmp.write_text(json.dumps(data))
        os.replace(tmp, p)


def github_token() -> str:
    try:
        return (SECRETS / "github-token").read_text().strip()
    except OSError:
        return ""


def all_runs() -> list[dict]:
    out = []
    for p in RUNS.glob("*/run.json"):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            pass
    return sorted(out, key=lambda r: r["created"], reverse=True)


def prune() -> None:
    terminal = [r for r in all_runs() if r.get("status") in ("done", "failed")]
    for r in terminal[KEEP:]:
        shutil.rmtree(RUNS / r["id"], ignore_errors=True)


def pr_base_ref(target: str, token: str) -> str | None:
    """The PR's real base branch (the CLI otherwise assumes 'main' and needs gh to know better); for a
    merged PR, the base commit at the time."""
    m = SLUG_RE.match(target)
    n = re.search(r"/pull/(\d+)", target)
    if not (m and n):
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{m[1]}/{m[2]}/pulls/{n[1]}",
                                 headers={"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            pr = json.load(r)
        # A merged PR's head is already in the base branch, so diff against where the base was then.
        return pr["base"]["sha"] if pr.get("merged") else pr["base"]["ref"]
    except (OSError, ValueError, KeyError):
        return None


def build(rid: str, spec: dict) -> tuple[list[str], dict]:
    """argv (never holds the key) and a clean environment for one run."""
    d = (RUNS / rid).resolve()
    (d / "home").mkdir(exist_ok=True)
    cfg = {
        "rules": {"enabled": True, "ruff": True, "eslint": False, "semgrep": True, "house_rules": True},
        "cloud": {"enabled": spec["cloud"] and not spec.get("local"), "provider": spec["provider"], "model": spec["model"],
                  "api_key_env": "CODECHECK_WEB_KEY", "effort": spec.get("effort"), "max_file_lines": 10**9, "audit_file_cap": 10**9, "skip_noise": True,
                  "extensions": CODE_EXTENSIONS},
        "thresholds": {"fail_on_severity": "high"},
    }
    if spec.get("local"):   # the user's own Ollama / LM Studio, reached through the URL they gave
        cfg["local"] = {"enabled": True, "provider": "openai_compatible", "base_url": spec["local_root"] + "/v1/chat/completions",
                        "model": spec["model"], "max_file_lines": 10**9, "skip_noise": True, "extensions": CODE_EXTENSIONS,
                        "request_timeout_seconds": 900}
    (d / "config.yaml").write_text(json.dumps(cfg))  # JSON is valid YAML
    target = ["diff", "--pr", spec["target"]] if spec["kind"] == "pr" else ["audit", "--repo-url", spec["target"]]
    argv = [sys.executable, "-c", "from codecheck.cli import app; app()", *target,
            "--config", str(d / "config.yaml"), "--output-dir", str(d / "out"), "--redact"]
    if spec["cloud"] and spec.get("local"):
        argv += ["--local", "--force-local"]
    elif spec["cloud"]:
        argv.append("--cloud")
    if spec.get("branch"):
        argv += ["--branch", spec["branch"]]
    if spec.get("skip_note"):
        argv += ["--skip-note", spec["skip_note"]]
    if spec["kind"] == "pr" and (base := pr_base_ref(spec["target"], spec.get("token", ""))):
        argv += ["--base-ref", base]
    env = {
        "PATH": f"{Path(sys.executable).parent}:{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(d / "home"), "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1", "COLUMNS": "110", "NO_COLOR": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
    }
    if spec["cloud"] and not spec.get("local"):
        env["CODECHECK_WEB_KEY"] = spec["key"]
    if spec.get("token"):  # env only: never in argv, the URL or on disk
        basic = base64.b64encode(f"x-access-token:{spec['token']}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")
    return argv, env


def scrub(text: str, spec: dict) -> str:
    for secret in (spec.get("key"), spec.get("token"), spec.get("local_url"), spec.get("local_root")):
        if secret:
            text = text.replace(secret, "***")
    return text


def preflight(spec: dict) -> str | None:
    """Why the AI review cannot run (bad provider, key or model), or None. The rules always run either way."""
    if spec.get("local"):   # no model name is typed: use the one the user's server has (loaded)
        spec["local_root"], err = local_root(spec["local_url"])
        if err:
            return err
        spec["model"], err, choices = detect_model(spec["local_root"], spec.get("model") or None)
        if choices:   # several loaded and none picked (the page asks first, so this is a server that changed since)
            return f"More than one model is loaded on your server ({', '.join(choices[:6])}). Run again and pick one."
        return err
    if spec["provider"] not in PROVIDERS:
        return "The provider is not one of the supported ones."
    if not spec["key"]:
        return "No API key was entered."
    if not MODEL_RE.match(spec["model"]):
        return f'"{spec["model"]}" is not a valid model ID (no spaces; use the ID, not the display name).'
    return check_model(spec["provider"], spec["model"], spec["key"])


def execute(rid: str, spec: dict) -> None:
    save(rid, status="running", started=time.time())
    logfile, stalled = RUNS / rid / "run.log", []
    logfile.write_text("")
    if spec["cloud"]:
        with logfile.open("a") as lf:
            lf.write("Checking the connection and model...\n" if spec.get("local") else "Checking the API key and model...\n")
        why = scrub(preflight(spec) or "", spec)
        with logfile.open("a") as lf:
            lf.write(f"AI review skipped: {why}\n" if why else "API key and model OK\n")
            if spec.get("local") and not why:
                lf.write(f"Using the model your server has loaded: {spec['model']}\n")
                save(rid, model=spec["model"])
        if why:  # still run the rules, and say in the report why the AI did not run
            spec["cloud"] = spec["local"] = False
            spec["skip_note"] = f"AI review did not run: {why}"
            save(rid, ai_error=why)
    argv, env = build(rid, spec)
    try:
        p = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, stdin=subprocess.DEVNULL,
                             errors="replace", start_new_session=True, cwd=RUNS / rid / "home")
        last = [time.monotonic()]

        def watch() -> None:
            while p.poll() is None:
                time.sleep(15)
                if time.monotonic() - last[0] > STALL:
                    stalled.append(1)
                    os.killpg(p.pid, signal.SIGKILL)
                    return
        threading.Thread(target=watch, daemon=True).start()
        with logfile.open("a") as lf:
            for line in p.stdout:
                last[0] = time.monotonic()
                lf.write(scrub(line, spec))
                lf.flush()
            if stalled:
                lf.write(f"\nStopped: no output for {STALL // 60} minutes.\n")
        code = 124 if stalled else p.wait()
    except OSError as e:
        logfile.write_text(f"Could not start: {e}")
        code = 127
    log = logfile.read_text(errors="replace")
    files = sorted(f.relative_to(RUNS / rid / "out").as_posix() for f in (RUNS / rid / "out").rglob("*")
                   if f.is_file() and f.suffix in (".md", ".docx", ".xlsx", ".json")) if (RUNS / rid / "out").exists() else []
    ai_problems = [m.group(0).strip(" -") for m in re.finditer(r"^\s*- (?:local|cloud)_llm: (?!\d+ file\(s\) not sent).+$", log, re.M)]
    extra = {"ai_error": "; ".join(ai_problems[:3])[:300]} if ai_problems and not spec.get("skip_note") else {}
    save(rid, status="done" if code in (0, 1) and files else "failed", code=code, log=log[-20000:],
         files=files, finished=time.time(), **extra)


def worker() -> None:
    while True:
        rid, spec = jobs.get()
        try:
            execute(rid, spec)
        except Exception as e:  # keep the worker alive
            save(rid, status="failed", log=f"Internal error: {type(e).__name__}")
        finally:
            spec.clear()  # drop the key from memory
            prune()


CSS = """:root{--bg:#fff;--fg:#1c1c1e;--mut:#6b6b73;--line:#d9d9de;--acc:#2f5fd0;--bad:#b3261e;--ok:#1b7f3b}
@media(prefers-color-scheme:dark){:root{--bg:#161618;--fg:#ececf0;--mut:#9a9aa3;--line:#35353b;--acc:#7da2ff;--bad:#ff8a80;--ok:#6fd48b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px}h1{font-size:1.5rem;margin:0 0 4px}h2{font-size:1.1rem;margin-top:32px}
p.s{color:var(--mut);margin:0 0 20px}label{display:block;margin:14px 0 4px;font-weight:600}
input,select{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
button{margin-top:18px;padding:10px 18px;border:0;border-radius:8px;background:var(--acc);color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:hover{opacity:.9}.row{display:flex;gap:12px}.row>div{flex:1}small{color:var(--mut)}a{color:var(--acc)}
.note{border:1px solid var(--line);border-radius:8px;padding:10px 12px;color:var(--mut);margin:16px 0}
.run{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--line)}
.run span:first-child{overflow-wrap:anywhere}.st{white-space:nowrap}.done{color:var(--ok)}.failed{color:var(--bad)}
pre{overflow:auto;max-height:520px;padding:12px;border:1px solid var(--line);border-radius:8px;font-size:.85rem;white-space:pre-wrap}
.files a{margin-right:14px}
details.how{border:1px solid var(--line);border-radius:10px;margin:14px 0;padding:0 14px}details.how>summary{padding:10px 0;font-weight:600;color:var(--fg)}
.how ol{padding-left:1.3em}.how li{margin:12px 0}.how code.blk{display:block;margin:6px 0;padding:8px 10px;border:1px solid var(--line);border-radius:8px;overflow-wrap:anywhere;font-size:.85rem}
.how code{word-break:normal;overflow-wrap:break-word}.how p{color:var(--mut)}
.steps{list-style:none;padding:0;margin:20px 0 8px}.steps li{padding:6px 0;color:var(--mut)}.steps b{display:inline-block;width:1.4em}
.steps .done{color:var(--ok)}.steps .active{color:var(--fg);font-weight:600}.steps .active b{color:var(--acc);animation:p 1s infinite}
.steps .failed{color:var(--bad)}@keyframes p{50%{opacity:.25}}summary{cursor:pointer;color:var(--mut)}
@media(prefers-reduced-motion:reduce){.steps .active b{animation:none}}
:root{--critical:#b3261e;--high:#d4561e;--medium:#b88400;--low:#2f5fd0;--info:#6b6b73}
@media(prefers-color-scheme:dark){:root{--critical:#ff8a80;--high:#ffab76;--medium:#f2c14e;--low:#7da2ff;--info:#9a9aa3}}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0 8px}.card{flex:1 1 90px;border:1px solid var(--line);border-radius:10px;padding:10px 12px;text-align:center}
.card span{display:block;font-size:1.6rem;font-weight:700;line-height:1.2}.card small{text-transform:uppercase;letter-spacing:.04em}
.card.big{flex-basis:130px}.card.big span{color:var(--ok)}.card.zero{opacity:.45}
.card.critical span{color:var(--critical)}.card.high span{color:var(--high)}.card.medium span{color:var(--medium)}.card.low span{color:var(--low)}.card.info span{color:var(--info)}
.note.ok{color:var(--ok)}.note.warn{color:var(--fg);border-color:var(--medium)}details.file{border:1px solid var(--line);border-radius:10px;margin:12px 0;padding:0 14px}
details.file>summary{padding:10px 0;overflow-wrap:anywhere}details.file>summary small{background:var(--line);border-radius:10px;padding:1px 8px;margin-left:6px;color:var(--fg)}
.finding{padding:10px 0;border-top:1px solid var(--line)}.fh{display:flex;gap:10px;align-items:baseline}
.badge{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;border:1px solid currentColor;border-radius:6px;padding:0 7px;white-space:nowrap}
.badge.critical{color:var(--critical)}.badge.high{color:var(--high)}.badge.medium{color:var(--medium)}.badge.low{color:var(--low)}.badge.info{color:var(--info)}
code{overflow-wrap:anywhere;word-break:break-all}.ex{margin:6px 0 0;white-space:pre-wrap;overflow-wrap:anywhere}pre.sug{margin:8px 0 0;max-height:260px}.skip{color:var(--mut)}"""

HOME = """<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Codecheck</title><style>{{css}}</style></head><body><main>
<h1>Codecheck</h1><p class=s>Automatic code review for a GitHub pull request or a whole repo.</p>
{% if err %}<div class="note failed">{{err}}</div>{% endif %}
{% if msg %}<div class="note warn">{{msg}}</div>{% endif %}
{% if busy %}<div class="note warn"><b>{{'The review lane is' if lanes==1 else 'All '+lanes|string+' review lanes are'}} busy right now.</b> {% if busy.waiting %}{{busy.waiting}} review{{'' if busy.waiting==1 else 's'}} already waiting. {% endif %}A review you submit now will wait {{busy.text}} before it starts. You can still submit: it keeps your place in line.</div>{% endif %}
<form method=post action="{{url_for('submit')}}">
<label for=target>GitHub URL</label>
<input id=target name=target required placeholder="https://github.com/owner/repo/pull/12  or  https://github.com/owner/repo" value="{{f.get('target','')}}">
<small>A pull request URL reviews that change. A repo URL audits the whole repo.</small>
<label for=branch>Branch <small>(repo audits only, blank = the repo's default branch)</small></label>
<input id=branch name=branch placeholder="e.g. develop" autocomplete=off value="{{f.get('branch','')}}">
<label for=cloud>AI review (optional)</label>
<select id=cloud name=cloud onchange="var v=this.value;document.getElementById('ai').hidden=v!='1';document.getElementById('lo').hidden=v!='2'">
<option value=0>Rules only (free, about a minute)</option>
<option value=1 {{'selected' if f.get('cloud')=='1'}}>Rules + AI model (your API key)</option>
<option value=2 {{'selected' if f.get('cloud')=='2'}}>Rules + my own local model (Ollama or LM Studio)</option></select>
<div id=ai {{'hidden' if f.get('cloud')!='1'}}>
<div class=row><div><label for=provider>Provider</label><select id=provider name=provider>
{% for p in providers %}<option {{'selected' if f.get('provider')==p}}>{{p}}</option>{% endfor %}</select></div>
<div><label for=model>Model</label><select id=model name=model></select></div>
<div><label for=effort>Effort</label><select id=effort name=effort>
{% for e in efforts %}<option {{'selected' if (f.get('effort') or 'medium')==e}}>{{e}}</option>{% endfor %}</select></div></div>
<small id=effnote>Lower effort is faster and cheaper, higher thinks longer. Models that cannot adjust it ignore it.</small>
<label for=key>API key</label><input id=key name=key type=password autocomplete=off>
<button type=button id=loadm style="margin-top:6px;padding:6px 12px;background:transparent;color:var(--acc);border:1px solid var(--acc)">Load every model my key can use</button> <small id=loadnote></small>
<script>(function(){var M={{models|tojson}},D={{providers|tojson}},W="{{f.get('model','')}}",p=document.getElementById('provider'),m=document.getElementById('model');
function fill(){m.innerHTML='';Object.keys(M[p.value]).forEach(function(id){var o=document.createElement('option');o.textContent=id;
o.selected=id==(W&&M[p.value][W]!==undefined?W:D[p.value]);m.appendChild(o)});W=''}
p.onchange=fill;fill();
var b=document.getElementById('loadm'),n=document.getElementById('loadnote'),k=document.getElementById('key');
b.onclick=function(){n.textContent='Loading...';var d=new FormData();d.append('provider',p.value);d.append('key',k.value);
fetch('/models',{method:'POST',body:d}).then(function(r){return r.json()}).then(function(j){
if(!j.models.length){n.textContent=j.error||'No models found.';return}
var cur=m.value;m.innerHTML='';j.models.forEach(function(id){var o=document.createElement('option');o.textContent=id;o.selected=id==cur;m.appendChild(o)});
n.textContent=j.models.length+' models loaded'}).catch(function(){n.textContent='Could not load the list.'})}})()</script>
<small>Used for this one run, never stored or logged. Code files are sent to the provider, one request per file (not workflow files, tests, bundles, empty or boilerplate files). Free keys often allow only about 20 requests a day.</small></div>
<div id=lo {{'hidden' if f.get('cloud')!='2'}}>
<label for=local_url>Address of your model server</label><input id=local_url name=local_url placeholder="https://your-tunnel.trycloudflare.com" autocomplete=off value="{{f.get('local_url','')}}">
{% if choices %}<label for=local_model>Which model should review the code?</label><select id=local_model name=local_model>
{% for m in choices %}<option {{'selected' if f.get('local_model')==m}}>{{m}}</option>{% endfor %}</select>
<small>Your server has more than one model loaded. Pick one, then press Run review again.</small>{% endif %}
<details class=how><summary>How to set this up (about 5 minutes)</summary>
<p>This review runs on our server, which cannot see <code>localhost</code> on your computer. A tunnel gives your model a public https address that only reaches your model server.</p>
<ol>
<li><b>Start your model server and load one model.</b> The review uses the model your server has loaded, so there is no model name to type. If several are loaded, the page asks which one to use. If none is, it asks you to load one.<br>
Ollama: open the app (or run <code>ollama serve</code>), then <code>ollama pull qwen2.5-coder:7b</code>. See what is loaded with <code>ollama ps</code>, and load one with <code>ollama run &lt;name&gt;</code>.<br>
LM Studio: load a model, open the Developer tab and start the server (port 1234).</li>
<li><b>Give the model a large context, 16384 tokens or more.</b> Otherwise long files are cut off and the answers get poor.
In Ollama set the <code>OLLAMA_CONTEXT_LENGTH=16384</code> environment variable before starting it. In LM Studio set Context Length when you load the model.</li>
<li><b>Install cloudflared</b> (free, no account).<br>
Mac: <code class=blk>brew install cloudflared</code>
Windows: <code class=blk>winget install Cloudflare.cloudflared</code>
Linux: download it from <code>github.com/cloudflare/cloudflared/releases</code>.</li>
<li><b>Open the tunnel and leave that window open.</b><br>
Ollama: <code class=blk>cloudflared tunnel --url http://localhost:11434 --http-host-header=localhost:11434</code>
LM Studio: <code class=blk>cloudflared tunnel --url http://localhost:1234</code></li>
<li><b>Copy the address it prints</b>, which looks like <code>https://random-words.trycloudflare.com</code>, and paste it in the box above. The page finds your model by itself.</li>
<li><b>Run the review, then close the tunnel</b> with Ctrl+C when it finishes.</li>
</ol>
<p><b>Good to know.</b> Your model server has no password, so anyone who finds the address can use it while the tunnel is open. Closing the tunnel ends that. Your code passes through Cloudflare on its way to your machine. A tunnel drops any request that takes longer than about 100 seconds, so a slow model on a big file will show under "Not checked". A smaller model or a GPU avoids that. One request is sent per code file.</p>
</details></div>
<button>Run review</button></form>
<div class=note>Trial service. There is no login: anyone with this page link can submit a review. Public repos, and private repos the server's read-only token can read. Reviews are not listed anywhere: keep the link you get after submitting.</div>
</main></body></html>"""

RUN = """<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
{% if r.status in ('queued','running') %}<noscript><meta http-equiv=refresh content=5></noscript>{% endif %}
<title>Codecheck run</title><style>{{css}}</style></head><body><main>
<p><a href="{{url_for('home')}}">&larr; New review</a></p>
<h1>{{r.target.replace('https://github.com/','')}}</h1>{% if r.branch %}<p class=s>branch <code>{{r.branch}}</code></p>{% endif %}
<p class=s>{{r.mode}}{{', AI: '+r.provider+(' · '+r.model if r.model else '') if r.cloud}} · <span id=st class="{{r.status}}">{{r.status}}</span></p>
<p class=note>This review is not listed anywhere. Anyone with this link can open it.</p>
{% if r.status in ('queued','running') %}<ol class=steps id=steps aria-live=polite></ol><p class=s id=el></p>
<details open><summary>Live output</summary><pre id=live></pre></details>
<script>
const LANES={{lanes}},sid="{{r.id}}",icon={done:"\u2713",active:"\u25cf",failed:"\u2715",todo:"\u25cb"};
async function tick(){try{const d=await (await fetch("/r/"+sid+"/status")).json();
document.getElementById("steps").innerHTML=d.steps.map(s=>`<li class="${s.state}"><b>${icon[s.state]}</b> ${s.name}${s.detail?` <small>(${s.detail})</small>`:""}</li>`).join("");
document.getElementById("el").textContent=d.status=="queued"?(d.queue&&!d.queue.busy?"A lane just freed up. Your review starts any moment now.":d.queue?`${LANES==1?"The review lane is":"All "+LANES+" review lanes are"} busy. You are number ${d.queue.pos} in line. Expected wait: ${d.queue.text}. This is an estimate, and your review starts by itself.`:"Waiting for a free lane."):`Running for ${d.elapsed} s. This page updates by itself.`;
const l=document.getElementById("live");l.textContent=d.log;l.scrollTop=l.scrollHeight;
if(d.status=="done"||d.status=="failed"){location.reload();return}}catch(e){}setTimeout(tick,2000)}
tick();
</script>{% endif %}
{% if r.files %}<p class=files>Download: {% for f in r.files %}<a href="{{url_for('download',rid=r.id,path=f)}}">{{{'docx':'Word','xlsx':'Excel','md':'Markdown','json':'JSON'}[f.rsplit('.',1)[1]]}}</a>{% endfor %}</p>{% endif %}
{% if r.ai_error %}<div class="note warn"><b>AI review did not run.</b> {{r.ai_error}}<br>The rules checks below are complete.</div>{% endif %}
{% if rep %}
<div class=cards>
{% if rep.compliance is not none %}<div class="card big"><span>{{'%.0f'|format(rep.compliance)}}%</span><small>compliance</small></div>{% endif %}
{% for sev,n in rep.counts %}<div class="card {{sev}} {{'zero' if not n}}"><span>{{n}}</span><small>{{sev}}</small></div>{% endfor %}</div>
<p class=s>{{rep.reviewed}} file{{'' if rep.reviewed==1 else 's'}} reviewed{% if rep.seconds %} in {{rep.seconds}} s{% endif %}{% if rep.when %} · {{rep.when}}{% endif %}<br>Checks run: {{rep.tiers|join(' + ')}}</p>
{% if not rep.total %}<div class="note ok">No findings. Nothing to fix.</div>{% endif %}
{% for file, items in rep.files.items() %}<details class=file {{'open' if rep.open or loop.index<=3}}><summary><code>{{file}}</code> <small>{{items|length}}</small></summary>
{% for f in items %}<div class=finding><div class=fh><span class="badge {{f.severity}}">{{f.severity}}</span><b>{{f.title}}</b></div>
<small>line {{f.line_start}}{% if f.line_end and f.line_end!=f.line_start %}&ndash;{{f.line_end}}{% endif %} · {{f.check_id}} ({{f.source}})</small>
{% if f.explanation and f.explanation!=f.title %}<p class=ex>{{f.explanation}}</p>{% endif %}
{% if f.suggestion %}<pre class=sug>{{f.suggestion}}</pre>{% endif %}</div>{% endfor %}</details>{% endfor %}
{% if rep.skipped %}<h2>Not checked</h2><ul class=skip>{% for x in rep.skipped %}<li>{{x}}</li>{% endfor %}</ul>{% endif %}
{% endif %}
{% if r.status=='failed' %}<h2>What went wrong</h2><pre>{{log}}</pre>{% endif %}
</main></body></html>"""


@app.get("/")
def home(err="", f=None, choices=None, msg=""):
    return render_template_string(HOME, css=CSS, providers=PROVIDERS, models=MODELS, efforts=EFFORTS, err=err, msg=msg, choices=choices or [], f=f or {}, busy=lanes_busy(), lanes=WORKERS)


@app.post("/models")
def models():
    """Every chat model the visitor's key can use, straight from the provider. The key is used for this one
    request and is neither stored nor logged."""
    provider, key = request.form.get("provider", ""), request.form.get("key", "").strip()
    if provider not in PROVIDERS or not key:
        return {"models": [], "error": "Enter the API key first."}, 400, {"Cache-Control": "no-store"}
    ids, err = list_models(provider, key)
    return {"models": ids, "error": err}, 200, {"Cache-Control": "no-store"}


@app.post("/run")
def submit():
    f = {k: request.form.get(k, "").strip() for k in ("target", "branch", "cloud", "provider", "model", "effort", "key", "local_url", "local_model")}
    target = f["target"].rstrip("/")
    kind = "pr" if PR_RE.match(target) else "repo" if REPO_RE.match(target) else None
    local = f["cloud"] == "2"                       # 0 = rules only, 1 = cloud AI, 2 = the user's own local model
    cloud = f["cloud"] in ("1", "2")
    model = f["local_model"] if local else f["model"] if MODEL_RE.match(f["model"]) else PROVIDERS.get(f["provider"], "")
    effort = f["effort"] if f["effort"] in EFFORTS else "medium"
    if local or not takes_effort(f["provider"], model):
        effort = None                               # this model has no effort setting: send nothing
    err = ""
    if not kind:
        err = "Enter a public GitHub pull request or repo URL, like https://github.com/owner/repo."
    elif f["branch"] and (not BRANCH_RE.match(f["branch"]) or ".." in f["branch"]):
        err = "That branch name has characters I do not accept. Use letters, numbers and . _ - / only."
    elif jobs.qsize() >= MAX_QUEUE:
        err = "The queue is full. Try again in a few minutes."
    if err:
        return home(err=err, f=f), 400, {"Cache-Control": "no-store"}
    if local:   # several models loaded on their machine: let them pick one before the review starts
        root, _ = local_root(f["local_url"])
        choices = detect_model(root, f["local_model"] or None)[2] if root else []
        if choices:
            return home(f=f, choices=choices, msg=("That model is not on your server. Pick one from the list." if f["local_model"]
                                                    else "Your server has more than one model loaded. Pick the one to use.")), 200
    rid = secrets.token_hex(6)
    branch = f["branch"] if kind == "repo" else ""     # a PR review always uses the PR's own branch
    with submit_lock:   # check and enqueue together, so a burst cannot pass MAX_QUEUE
        if jobs.qsize() >= MAX_QUEUE:
            return home(err="The queue is full. Try again in a few minutes.", f=f), 400, {"Cache-Control": "no-store"}
        (RUNS / rid).mkdir(parents=True)
        save(rid, id=rid, created=time.time(), status="queued", target=target, kind=kind, cloud=cloud, branch=branch,
             provider="local" if local else f["provider"] if cloud else "", model=model if cloud else "", ai_error="",
             local=local,
             mode="PR review" if kind == "pr" else "Repo audit", files=[], log="")
        jobs.put((rid, {"kind": kind, "target": target, "branch": branch, "cloud": cloud, "local": local,
                        "provider": "local" if local else f["provider"], "model": model, "effort": None if local else effort, "key": "" if local else f["key"],
                        "local_url": f["local_url"] if local else "", "token": github_token()}))
    return redirect(url_for("show", rid=rid))


def public_log(text: str) -> str:
    """Output safe to show visitors: no server paths, and nothing after the report-file list."""
    text = text.split("\nReports written")[0]
    if m := re.search(r"Remote branch (\S+) not found", text):
        return f'The branch "{m[1]}" does not exist in this repo. Check the spelling, or leave the branch blank for the default branch.'
    text = re.sub(r"/tmp/codecheck-clone-\w+", "<repo>", text)
    for path, label in ((str(RUNS), "<runs>"), (str(Path.home()), "~")):
        text = text.replace(path, label)
    return ANSI_RE.sub("", text)


SEVERITIES = ["critical", "high", "medium", "low", "info"]
TIER_NAMES = {"rules": "Rules (ruff, semgrep, house rules)", "cloud_llm": "AI model", "local_llm": "Local AI model"}


def report_view(rid: str, r: dict) -> dict | None:
    """The findings of a finished run, shaped for the page (read from the JSON report, not the Markdown)."""
    name = next((f for f in r.get("files", []) if f.endswith(".json")), None)
    try:
        d = json.loads((RUNS / rid / "out" / name).read_text()) if name else None
    except (OSError, ValueError):
        d = None
    if not d:
        return None
    by_file: dict[str, list] = {}
    for f in sorted(d.get("findings", []), key=lambda x: (SEVERITIES.index(x.get("severity", "info")) if x.get("severity") in SEVERITIES else 9, x.get("line_start", 0))):
        by_file.setdefault(f.get("file", ""), []).append(f)
    counts = d.get("counts_by_severity", {})
    try:
        when = datetime.fromisoformat(d["generated_at"]).astimezone().strftime("%d %b %Y, %H:%M")
    except (KeyError, ValueError):
        when = ""
    total = sum(len(v) for v in by_file.values())
    return {"counts": [(sev, counts.get(sev, 0)) for sev in SEVERITIES], "total": total, "files": by_file,
            "compliance": d.get("compliance_percentage"), "reviewed": len(d.get("file_hashes", {})) or len(d.get("files_reviewed", [])),
            "seconds": round(d.get("duration_seconds", 0)), "when": when, "skipped": d.get("skipped", []),
            "tiers": [TIER_NAMES.get(t, t) for t in d.get("tiers_run", [])], "open": total <= 60}


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


CHECK_STEP = {False: "Checking the API key and model", True: "Checking the connection to your model"}


def progress(r: dict, log: str, pos: int) -> dict:
    """Steps and a one-line detail for the status page, read from what the CLI has printed so far."""
    ai = r["cloud"] and not r.get("ai_error")
    names = ["Waiting in the queue"] + ([CHECK_STEP[bool(r.get("local"))]] if r["cloud"] else []) + [
        "Fetching the code from GitHub", "Running the rules (ruff, semgrep, house rules)"] + (["AI review"] if ai else []) + [
        "Writing the report"]
    at = {n: i for i, n in enumerate(names)}
    detail = {}
    if r["status"] == "queued":
        cur = 0
        detail[0] = f"position {pos}" if pos > 1 else "next up"
    else:
        cur = at["Fetching the code from GitHub"]
        if r["cloud"] and "API key and model OK" not in log and "AI review skipped" not in log:
            cur = at[CHECK_STEP[bool(r.get("local"))]]
        if "Running rules" in log:
            cur = at["Running the rules (ruff, semgrep, house rules)"]
        if ai and "AI review:" in log:
            cur = at["AI review"]
            m = re.findall(r"\((\d+)/(\d+)\)", log)
            total = re.findall(r"AI review: (\d+) file", log)
            detail[cur] = f"{m[-1][0]} of {m[-1][1]} files" if m else f"{total[-1]} files" if total else ""
        if "Reports written" in log:
            cur = len(names) - 1
        if r.get("ai_error"):
            detail[at[CHECK_STEP[bool(r.get("local"))]]] = "AI review skipped, rules will still run"
    state = {"done": "done", "failed": "failed"}.get(r["status"])
    steps = [{"name": n, "detail": detail.get(i, ""),
              "state": ("done" if state == "done" else "failed" if state == "failed" and i == cur
                        else "done" if i < cur else "active" if i == cur and not state else "todo")}
             for i, n in enumerate(names)]
    end = r.get("finished") or time.time()
    return {"status": r["status"], "steps": steps, "elapsed": round(end - r.get("started", end)) if r.get("started") else 0,
            "log": public_log(log)[-3000:]}


def read_log(rid: str, r: dict) -> str:
    try:
        return (RUNS / rid / "run.log").read_text(errors="replace")
    except OSError:
        return r.get("log", "")


def queue_pos(r: dict) -> int:
    return sum(1 for x in all_runs() if x["status"] == "queued" and x["created"] <= r["created"])


GUESS = {("pr", False): 45, ("pr", True): 90, ("repo", False): 60, ("repo", True): 240}   # seconds, until real runs exist


def typical(run: dict, finished: list[dict]) -> float:
    """How long a run like this usually takes: the median of the recent finished ones of the same kind."""
    key = (run.get("kind"), bool(run.get("cloud")))
    secs = sorted(x["finished"] - x["started"] for x in finished if (x.get("kind"), bool(x.get("cloud"))) == key)[-20:]
    return secs[len(secs) // 2] if secs else GUESS.get(key, 90)


def wait_seconds(ahead: list[dict], new: dict, runs: list[dict] | None = None) -> float:
    """Estimated seconds until `new` starts: each lane is free when its current run is expected to end,
    and the runs queued ahead take the earliest lane in turn."""
    runs = all_runs() if runs is None else runs
    finished = [x for x in runs if x["status"] == "done" and x.get("started") and x.get("finished")]
    now = time.time()
    lanes = sorted([max(typical(x, finished) - (now - x.get("started", now)), 30) for x in runs if x["status"] == "running"][:WORKERS]
                   + [0.0] * max(WORKERS - sum(1 for x in runs if x["status"] == "running"), 0))
    for job in ahead:
        lanes[0] += typical(job, finished)
        lanes.sort()
    return lanes[0]


def human(seconds: float) -> str:
    if seconds < 45:
        return "less than a minute"
    if seconds < 90:
        return "about a minute"
    if seconds < 3600:
        return f"about {round(seconds / 60)} minutes"
    return "over an hour"


def queue_info(r: dict) -> dict | None:
    """Where a queued run stands: its place in line and a rough wait, or None once it is running."""
    if r["status"] != "queued":
        return None
    runs = all_runs()
    ahead = sorted((x for x in runs if x["status"] == "queued" and x["created"] < r["created"]), key=lambda x: x["created"])
    busy = sum(1 for x in runs if x["status"] == "running") >= WORKERS
    return {"pos": len(ahead) + 1, "text": human(wait_seconds(ahead, r, runs)), "busy": busy}


def lanes_busy() -> dict | None:
    """Set when every lane is taken, for the form: how many are waiting and how long a new review would wait."""
    runs = all_runs()
    if sum(1 for x in runs if x["status"] == "running") < WORKERS:
        return None
    queued = sorted((x for x in runs if x["status"] == "queued"), key=lambda x: x["created"])
    return {"waiting": len(queued), "text": human(wait_seconds(queued, {"kind": "pr", "cloud": False}, runs))}


@app.get("/r/<rid>/status")
def status(rid):
    r = load(rid)
    return {**progress(r, read_log(rid, r), queue_pos(r)), "queue": queue_info(r)}


@app.get("/r/<rid>")
def show(rid):
    r = load(rid)
    return render_template_string(RUN, css=CSS, lanes=WORKERS, r=r, rep=report_view(rid, r) if r["status"] == "done" else None, log=public_log(r.get("log", "")))


@app.get("/r/<rid>/f/<path:path>")
def download(rid, path):
    if path not in load(rid).get("files", []):
        abort(404)
    return send_from_directory(RUNS / rid / "out", path, as_attachment=not path.endswith(".md"))


def main() -> None:
    RUNS.mkdir(parents=True, exist_ok=True)
    for r in all_runs():  # a restart loses the queue
        if r["status"] in ("queued", "running"):
            save(r["id"], status="failed", log="Service restarted before this run finished. Run it again.")
    for _ in range(WORKERS):
        threading.Thread(target=worker, daemon=True).start()
    from waitress import serve
    serve(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8090")), threads=8)


if __name__ == "__main__":
    main()
