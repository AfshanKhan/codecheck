import subprocess
from pathlib import Path

import pytest

from codecheck.config import RulesConfig
from codecheck.diff import get_diff
from codecheck.reviewers.rules_engine import RulesEngineReviewer


@pytest.fixture
def sandbox_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sandbox"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")

    (repo / "a.py").write_text("def foo():\n    return 1\n")
    run("add", "a.py")
    run("commit", "-q", "-m", "initial")
    run("branch", "-m", "main")

    run("checkout", "-q", "-b", "feature")
    # unused import (F401) on a changed line, plus a clean line
    (repo / "a.py").write_text("import os\n\n\ndef foo():\n    return 1\n")
    run("add", "-A")
    run("commit", "-q", "-m", "feature change")

    return repo


def test_ruff_flags_unused_import_on_changed_line(sandbox_repo: Path):
    changed_files = get_diff(sandbox_repo, base_ref="main", staged=False)
    reviewer = RulesEngineReviewer(RulesConfig(ruff=True, eslint=False, semgrep=False, house_rules=False))

    available, reason = reviewer.is_available(sandbox_repo)
    assert available, reason

    findings = reviewer.review(changed_files, sandbox_repo)
    assert any(f.check_id == "RUFF-F401" and f.file == "a.py" and f.line_start == 1 for f in findings)


def test_ruff_ignores_findings_outside_changed_lines(tmp_path: Path):
    repo = tmp_path / "sandbox2"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")

    # pre-existing unused import on main, untouched by the feature branch
    (repo / "a.py").write_text("import os\n\n\ndef foo():\n    return 1\n")
    run("add", "a.py")
    run("commit", "-q", "-m", "initial")
    run("branch", "-m", "main")

    run("checkout", "-q", "-b", "feature")
    (repo / "a.py").write_text("import os\n\n\ndef foo():\n    return 2\n")
    run("add", "-A")
    run("commit", "-q", "-m", "feature change")

    changed_files = get_diff(repo, base_ref="main", staged=False)
    reviewer = RulesEngineReviewer(RulesConfig(ruff=True, eslint=False, semgrep=False, house_rules=False))
    findings = reviewer.review(changed_files, repo)

    assert not any(f.check_id == "RUFF-F401" for f in findings)


def test_review_records_enabled_but_skipped_runners(monkeypatch):
    # ruff and semgrep are enabled but their binaries aren't on PATH. They must
    # be recorded in skipped_runners (so the CLI can surface them), not silently
    # mistaken for "ran and found nothing."
    monkeypatch.setattr(
        "codecheck.reviewers.rules_engine.shutil.which", lambda _name: None
    )
    reviewer = RulesEngineReviewer(
        RulesConfig(ruff=True, eslint=False, semgrep=True, house_rules=False)
    )

    findings = reviewer.review([], Path("."))

    assert findings == []
    skipped = dict(reviewer.skipped_runners)
    assert "ruff" in skipped
    assert "semgrep" in skipped
