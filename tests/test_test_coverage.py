import subprocess
from pathlib import Path

import pytest

from codecheck.config import RulesConfig
from codecheck.diff import get_diff
from codecheck.models import ReviewTarget
from codecheck.reviewers.rules_engine import RulesEngineReviewer, TestCoverageRunner, _is_test_path


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def sandbox_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sandbox"
    repo.mkdir()
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("def foo():\n    return 1\n")
    (repo / "test_app.py").write_text("def test_foo():\n    assert True\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "initial")
    _run(repo, "branch", "-m", "main")
    _run(repo, "checkout", "-q", "-b", "feature")
    return repo


def _reviewer() -> RulesEngineReviewer:
    return RulesEngineReviewer(
        RulesConfig(ruff=False, eslint=False, semgrep=False, house_rules=False, test_coverage=True)
    )


def test_flags_substantial_app_change_with_no_test_touch(sandbox_repo: Path):
    (sandbox_repo / "app.py").write_text(
        "def foo():\n    return 1\n\n\n"
        + "\n".join(f"def helper_{i}():\n    return {i}" for i in range(10))
        + "\n"
    )
    _run(sandbox_repo, "add", "-A")
    _run(sandbox_repo, "commit", "-q", "-m", "add several helpers")

    changed_files = get_diff(sandbox_repo, base_ref="main", staged=False)
    findings = _reviewer().review(changed_files, sandbox_repo)
    assert any(f.check_id == "RULE-017" for f in findings)


def test_not_flagged_when_test_file_updated_with_real_test(sandbox_repo: Path):
    (sandbox_repo / "app.py").write_text(
        "def foo():\n    return 1\n\n\n"
        + "\n".join(f"def helper_{i}():\n    return {i}" for i in range(10))
        + "\n"
    )
    (sandbox_repo / "test_app.py").write_text(
        "def test_foo():\n    assert True\n\n\ndef test_helper_0():\n    assert True\n"
    )
    _run(sandbox_repo, "add", "-A")
    _run(sandbox_repo, "commit", "-q", "-m", "add helpers and a test")

    changed_files = get_diff(sandbox_repo, base_ref="main", staged=False)
    findings = _reviewer().review(changed_files, sandbox_repo)
    assert not any(f.check_id == "RULE-017" for f in findings)


def test_small_change_not_flagged(sandbox_repo: Path):
    (sandbox_repo / "app.py").write_text("def foo():\n    return 2\n")
    _run(sandbox_repo, "add", "-A")
    _run(sandbox_repo, "commit", "-q", "-m", "tiny tweak")

    changed_files = get_diff(sandbox_repo, base_ref="main", staged=False)
    findings = _reviewer().review(changed_files, sandbox_repo)
    assert not any(f.check_id == "RULE-017" for f in findings)


def test_flags_substantial_tsx_change_with_no_test_touch():
    # regression (Greptile): _APP_EXTENSIONS originally omitted .jsx/.ts/.tsx,
    # even though the eslint sub-runner already supports them.
    diff_text = "\n".join(f"+function Helper{i}() {{ return {i}; }}" for i in range(10))
    targets = [
        ReviewTarget(path="component.tsx", status="modified", diff_text=diff_text, changed_lines={1}),
    ]
    findings = TestCoverageRunner().run(targets, Path("."))
    assert any(f.check_id == "RULE-017" for f in findings)


def test_flags_substantial_change_when_touched_file_only_contains_test_substring():
    # regression (Greptile): a plain "test" in path substring match wrongly
    # classified ordinary application files like contest.tsx/latest.ts as
    # tests, hiding a real missing-test finding.
    diff_text = "\n".join(f"+function Helper{i}() {{ return {i}; }}" for i in range(10))
    targets = [
        ReviewTarget(path="contest.tsx", status="modified", diff_text=diff_text, changed_lines={1}),
    ]
    findings = TestCoverageRunner().run(targets, Path("."))
    assert any(f.check_id == "RULE-017" for f in findings)


@pytest.mark.parametrize(
    "path",
    [
        "test_app.py",
        "app_test.py",
        "src/app/test_app.py",
        "component.test.tsx",
        "component.spec.ts",
        "tests/app.py",
        "__tests__/component.jsx",
    ],
)
def test_is_test_path_matches_real_test_conventions(path: str):
    assert _is_test_path(path)


@pytest.mark.parametrize("path", ["contest.tsx", "latest.ts", "attestation.py", "testament.js"])
def test_is_test_path_does_not_match_ordinary_app_files(path: str):
    assert not _is_test_path(path)


def test_audit_mode_is_a_no_op():
    targets = [
        ReviewTarget(path="app.py", status="scanned", diff_text="", changed_lines=None),
    ]
    findings = TestCoverageRunner().run(targets, Path("."))
    assert findings == []
