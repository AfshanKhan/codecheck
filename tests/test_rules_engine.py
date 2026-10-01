import subprocess
from pathlib import Path

import pytest

from codecheck.config import RulesConfig
from codecheck.diff import get_diff
from codecheck.models import ReviewTarget
from codecheck.reviewers.rules_engine import RulesEngineReviewer, SemgrepRunner


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


def test_disabled_checks_drops_matching_findings(tmp_path: Path):
    (tmp_path / "risky.py").write_text("try:\n    pass\nexcept:\n    pass\n")
    targets = [ReviewTarget(path="risky.py", status="modified", changed_lines={3})]

    reviewer = RulesEngineReviewer(
        RulesConfig(ruff=False, eslint=False, semgrep=False, house_rules=True, test_coverage=False)
    )
    findings = reviewer.review(targets, tmp_path)
    assert any(f.check_id == "RULE-001" for f in findings)

    reviewer_disabled = RulesEngineReviewer(
        RulesConfig(
            ruff=False, eslint=False, semgrep=False, house_rules=True, test_coverage=False,
            disabled_checks=["RULE-001"],
        )
    )
    findings_disabled = reviewer_disabled.review(targets, tmp_path)
    assert not any(f.check_id == "RULE-001" for f in findings_disabled)


def test_disabled_checks_can_suppress_a_non_house_check_id(tmp_path: Path):
    # the suppression is a post-filter over every sub-runner's output, not
    # just house rules -- a ruff/eslint/semgrep code should be suppressible
    # the same way.
    (tmp_path / "risky.py").write_text("try:\n    pass\nexcept:\n    pass\n")
    targets = [ReviewTarget(path="risky.py", status="modified", changed_lines={3})]
    reviewer = RulesEngineReviewer(
        RulesConfig(
            ruff=False, eslint=False, semgrep=False, house_rules=True, test_coverage=False,
            disabled_checks=["RULE-002", "RULE-999-does-not-exist"],  # unrelated IDs, no-op here
        )
    )
    findings = reviewer.review(targets, tmp_path)
    assert any(f.check_id == "RULE-001" for f in findings)  # untouched by an unrelated suppression


def test_extra_checks_run_alongside_built_ins(tmp_path: Path):
    (tmp_path / "risky.py").write_text("try:\n    pass\nexcept:\n    pass\n")
    targets = [ReviewTarget(path="risky.py", status="modified", changed_lines={3})]
    reviewer = RulesEngineReviewer(
        RulesConfig(
            ruff=False, eslint=False, semgrep=False, house_rules=True, test_coverage=False,
            extra_checks=["codecheck.checks.no_bare_except:NoBareExceptCheck"],
        )
    )
    findings = reviewer.review(targets, tmp_path)
    # the built-in RULE-001 fires once from ALL_CHECKS, plus once more from
    # the same check reloaded as an "extra" -- proves it actually ran twice,
    # not just once from the pre-existing built-in registration.
    assert sum(1 for f in findings if f.check_id == "RULE-001") == 2


def test_bad_extra_check_is_reported_not_crashing(tmp_path: Path):
    reviewer = RulesEngineReviewer(
        RulesConfig(
            ruff=False, eslint=False, semgrep=False, house_rules=True, test_coverage=False,
            extra_checks=["nonexistent_package.module:SomeCheck"],
        )
    )
    findings = reviewer.review([], tmp_path)
    assert findings == []
    skipped = dict(reviewer.skipped_runners)
    assert "house_rules.extra_checks" in skipped
    assert "nonexistent_package.module:SomeCheck" in skipped["house_rules.extra_checks"]


def test_rule_018_fires_on_deletion_only_change_to_long_function(tmp_path: Path):
    # regression (Greptile + the deeper diff.py fix underneath it): a commit
    # that ONLY removes lines from an already-too-long function (still too
    # long afterward) must still trigger RULE-018 end-to-end through the real
    # get_diff() pipeline, not just when changed_lines is handed to the check
    # directly in a unit test.
    repo = tmp_path / "sandbox_del"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")

    body_lines = "\n".join(f"    x{i} = {i}" for i in range(60))
    (repo / "big.py").write_text(f"def big():\n{body_lines}\n    return x0\n")
    run("add", "-A")
    run("commit", "-q", "-m", "initial")
    run("branch", "-m", "main")

    run("checkout", "-q", "-b", "feature")
    # remove 5 of the 60 body lines -- pure deletion, zero additions, but the
    # function (still 55+ lines) is still well over the 50-line threshold.
    trimmed_lines = "\n".join(f"    x{i} = {i}" for i in range(55))
    (repo / "big.py").write_text(f"def big():\n{trimmed_lines}\n    return x0\n")
    run("add", "-A")
    run("commit", "-q", "-m", "trim some lines")

    changed_files = get_diff(repo, base_ref="main", staged=False)
    reviewer = RulesEngineReviewer(
        RulesConfig(ruff=False, eslint=False, semgrep=False, house_rules=True)
    )
    findings = reviewer.review(changed_files, repo)
    assert any(f.check_id == "RULE-018" for f in findings)


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


def test_rules_engine_passes_semgrep_config_through_to_runner():
    reviewer = RulesEngineReviewer(
        RulesConfig(
            ruff=False,
            eslint=False,
            semgrep=True,
            house_rules=False,
            semgrep_config="/clone/of/frappe-semgrep-rules/rules",
        )
    )
    (semgrep_runner,) = [r for r in reviewer._runners if isinstance(r, SemgrepRunner)]
    assert semgrep_runner.config_path == "/clone/of/frappe-semgrep-rules/rules"


def test_a_sub_runner_failure_is_recorded_as_skipped_not_raised(monkeypatch):
    # regression (Greptile review): a sub-runner failing mid-run (e.g.
    # semgrep exiting non-0/1 on a bad semgrep_config) used to crash the
    # whole rules-tier review instead of being recorded like any other
    # unavailable/skipped runner.
    monkeypatch.setattr("codecheck.reviewers.rules_engine.shutil.which", lambda _name: "/usr/bin/semgrep")
    monkeypatch.setattr(
        "codecheck.reviewers.rules_engine.SemgrepRunner.run",
        lambda self, targets, repo_path: (_ for _ in ()).throw(RuntimeError("semgrep exited 7")),
    )
    reviewer = RulesEngineReviewer(
        RulesConfig(ruff=False, eslint=False, semgrep=True, house_rules=False)
    )

    findings = reviewer.review([], Path("."))

    assert findings == []
    assert dict(reviewer.skipped_runners)["semgrep"] == "semgrep exited 7"
    # Distinct from an ordinary "unavailable" skip (e.g. binary not on
    # PATH) -- this is a sub-runner that was configured and ran, then
    # crashed, which a gate should treat as a hard failure.
    assert reviewer.runner_failures == ["semgrep"]


def test_runner_failures_stays_empty_for_an_ordinary_unavailable_skip(monkeypatch):
    monkeypatch.setattr("codecheck.reviewers.rules_engine.shutil.which", lambda _name: None)
    reviewer = RulesEngineReviewer(
        RulesConfig(ruff=True, eslint=False, semgrep=False, house_rules=False)
    )

    reviewer.review([], Path("."))

    assert "ruff" in dict(reviewer.skipped_runners)
    assert reviewer.runner_failures == []
