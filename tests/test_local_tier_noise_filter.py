"""The local tier with skip_noise and extensions set, end to end through the CLI, against a fake OpenAI-compatible server."""
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from typer.testing import CliRunner

from codecheck.cli import app

seen: list[str] = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "qwen:7b"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["content-length"])))
        text = req["messages"][-1]["content"]
        seen.append(next(line for line in text.splitlines() if "logic.py" in line or "test_a" in line or "__init__" in line or "ci.yml" in line))
        args = {"findings": [{"severity": "low", "title": "Local model says hi", "explanation": "x", "line_start": 1}]}
        body = json.dumps({"choices": [{"message": {"tool_calls": [{"function": {"name": "report_findings", "arguments": json.dumps(args)}}]}}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)


def test_local_model_gets_only_real_code_and_the_note_lists_what_was_left_out(tmp_path):
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    repo = tmp_path / "repo"
    for name, src in {"app/logic.py": "def f():\n    return 1\n", "app/__init__.py": "", "tests/test_a.py": "def test_a():\n    assert 1\n",
                      ".github/workflows/ci.yml": "on: push\n", "app/notes.json": "{}"}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(src)
    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(json.dumps({"rules": {"enabled": False},
                               "local": {"enabled": True, "provider": "openai_compatible", "model": "qwen:7b", "max_file_lines": 10**9,
                                         "base_url": f"http://127.0.0.1:{srv.server_port}/v1/chat/completions",
                                         "skip_noise": True, "extensions": [".py", ".js", ".ts", ".html"]}}))
    seen.clear()
    r = CliRunner().invoke(app, ["audit", "--repo-path", str(repo), "--config", str(cfg), "--local", "--force-local",
                                 "--output-dir", str(tmp_path / "out")])
    srv.shutdown()
    assert len(seen) == 1 and "logic.py" in seen[0], (seen, r.output)
    report = json.loads(next((tmp_path / "out").glob("*/*.json")).read_text())
    assert any(f["tier"] == "local_llm" and f["title"] == "Local model says hi" for f in report["findings"])
    assert any(x.startswith("local_llm: 2 file(s) not sent to the AI (") for x in report["skipped"])    # tests + empty (ci.yml/json never listed)
