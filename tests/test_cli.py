import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codecheck.cli import _run_llm_tier, app
from codecheck.lm_link import DeviceCandidate, ModelLocation
from codecheck.models import ReviewTarget

runner = CliRunner()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def sandbox_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sandbox"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    (repo / "risky.py").write_text(
        "import frappe\n\n"
        "def get_thing(name):\n"
        "    try:\n"
        '        return frappe.db.sql(f"select * from tabThing where name = {name}")\n'
        "    except:\n"
        "        return None\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "branch", "-m", "main")
    return repo


def test_audit_finds_house_rule_violations(sandbox_repo: Path, tmp_path: Path):
    output_dir = tmp_path / "reports"
    result = runner.invoke(
        app, ["audit", "--repo-path", str(sandbox_repo), "--output-dir", str(output_dir)]
    )

    assert result.exit_code == 1  # RULE-002 is HIGH, hits the default fail_on_severity
    assert "RULE-002" in result.stdout
    assert (output_dir / "report.json").exists()
    assert (output_dir / "report.md").exists()


def test_audit_local_without_model_configured_skips_gracefully(sandbox_repo: Path, tmp_path: Path):
    output_dir = tmp_path / "reports"
    result = runner.invoke(
        app, ["audit", "--repo-path", str(sandbox_repo), "--local", "--output-dir", str(output_dir)]
    )

    assert result.exit_code == 1  # rules tier still finds RULE-002
    assert "local.model not set" in result.stdout
    assert "LOCAL-" not in result.stdout

    report = json.loads((output_dir / "report.json").read_text())
    assert "local_llm" not in report["tiers_run"]


def test_audit_local_ollama_skips_lm_link_gate_entirely(sandbox_repo: Path, tmp_path: Path):
    # Ollama has no LM Link / multi-device concept, so the whole device
    # resolution + confirmation gate should be bypassed -- no "Local LLM
    # tier: ..." message, no interactive/non-interactive gating at all, the
    # reviewer just runs (and fails per-file on the unreachable port here).
    output_dir = tmp_path / "reports"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "rules:\n  enabled: false\nlocal:\n  enabled: true\n  provider: ollama\n"
        "  model: qwen2.5-coder\n  base_url: http://127.0.0.1:19999/v1/chat/completions\n"
    )

    with patch("codecheck.cli.resolve_model_location") as mock_resolve:
        result = runner.invoke(
            app,
            [
                "audit", "--repo-path", str(sandbox_repo),
                "--config", str(config_path), "--output-dir", str(output_dir),
            ],
        )

    mock_resolve.assert_not_called()
    assert "Local LLM tier:" not in result.stdout
    assert result.exit_code == 0
    report = json.loads((output_dir / "report.json").read_text())
    assert "local_llm" in report["tiers_run"]
    assert any("API request failed" in s for s in report["skipped"])


def test_diff_local_undetermined_device_skips_without_force_local(sandbox_repo: Path, tmp_path: Path):
    _git(sandbox_repo, "checkout", "-q", "-b", "feature")
    (sandbox_repo / "clean.py").write_text("x = 1\n")
    _git(sandbox_repo, "add", "-A")
    _git(sandbox_repo, "commit", "-q", "-m", "clean change")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "local:\n  enabled: true\n  model: qwen2.5-coder\n"
        "  base_url: http://127.0.0.1:19999/v1/chat/completions\n"
    )
    output_dir = tmp_path / "reports"

    with patch(
        "codecheck.cli.resolve_model_location",
        return_value=ModelLocation(is_local=None, description="not found anywhere"),
    ):
        result = runner.invoke(
            app,
            [
                "diff", "--repo-path", str(sandbox_repo), "--base-ref", "main",
                "--config", str(config_path), "--output-dir", str(output_dir),
            ],
        )

    # non-interactive (CliRunner) + undetermined device + no --force-local:
    # the local tier must be skipped before any HTTP call is attempted, not crash
    assert result.exit_code == 0
    assert "pass --force-local" in result.stdout
    report = json.loads((output_dir / "report.json").read_text())
    assert "local_llm" not in report["tiers_run"]


def test_diff_local_confirmed_remote_device_proceeds_without_force_local(
    sandbox_repo: Path, tmp_path: Path
):
    _git(sandbox_repo, "checkout", "-q", "-b", "feature")
    (sandbox_repo / "clean.py").write_text("x = 1\n")
    _git(sandbox_repo, "add", "-A")
    _git(sandbox_repo, "commit", "-q", "-m", "clean change")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "local:\n  enabled: true\n  model: qwen2.5-coder\n"
        # an always-unreachable port -- we only care that it attempted the
        # call at all (proving it wasn't skipped by the confirmation gate),
        # not that the call itself succeeds
        "  base_url: http://127.0.0.1:19999/v1/chat/completions\n"
    )
    output_dir = tmp_path / "reports"

    with patch(
        "codecheck.cli.resolve_model_location",
        return_value=ModelLocation(
            is_local=False, description="on remote device 'test-device'", device_name="test-device"
        ),
    ):
        result = runner.invoke(
            app,
            [
                "diff", "--repo-path", str(sandbox_repo), "--base-ref", "main",
                "--config", str(config_path), "--output-dir", str(output_dir),
            ],
        )

    assert result.exit_code == 0
    report = json.loads((output_dir / "report.json").read_text())
    # confirmed remote -> tier actually ran (and then failed per-file on the
    # unreachable port, which is a skip-not-crash, not a tier-level skip)
    assert "local_llm" in report["tiers_run"]
    assert any("API request failed" in s for s in report["skipped"])
    assert (output_dir / "report.json").exists()


def _ambiguous_location() -> ModelLocation:
    return ModelLocation(
        is_local=None,
        description="ambiguous",
        is_ambiguous=True,
        candidates=[
            DeviceCandidate(label="Local (this machine)", device_identifier="local-id"),
            DeviceCandidate(label="Some-Remote-Box", device_identifier="remote-id"),
        ],
    )


def test_audit_ambiguous_device_without_flag_skips_non_interactively(
    sandbox_repo: Path, tmp_path: Path
):
    output_dir = tmp_path / "reports"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("rules:\n  enabled: false\nlocal:\n  enabled: true\n  model: some/model\n")

    with patch("codecheck.cli.resolve_model_location", return_value=_ambiguous_location()):
        result = runner.invoke(
            app,
            [
                "audit", "--repo-path", str(sandbox_repo),
                "--config", str(config_path), "--output-dir", str(output_dir),
            ],
        )

    assert result.exit_code == 0
    assert "pass --device" in result.stdout
    report = json.loads((output_dir / "report.json").read_text())
    assert "local_llm" not in report["tiers_run"]


def test_audit_ambiguous_device_with_matching_device_flag_sets_preference(
    sandbox_repo: Path, tmp_path: Path
):
    output_dir = tmp_path / "reports"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "rules:\n  enabled: false\nlocal:\n  enabled: true\n  model: some/model\n"
        "  base_url: http://127.0.0.1:19999/v1/chat/completions\n"
    )

    with patch("codecheck.cli.resolve_model_location", return_value=_ambiguous_location()), \
         patch("codecheck.cli.set_preferred_device", return_value=(True, "ok")) as mock_set:
        result = runner.invoke(
            app,
            [
                "audit", "--repo-path", str(sandbox_repo),
                "--config", str(config_path), "--output-dir", str(output_dir),
                "--device", "Some-Remote-Box",
            ],
        )

    mock_set.assert_called_once_with("remote-id")
    assert result.exit_code == 0
    report = json.loads((output_dir / "report.json").read_text())
    # the tier actually ran (proceeded past the ambiguity gate), then failed
    # per-file on the unreachable port -- proving it wasn't skipped by the gate
    assert "local_llm" in report["tiers_run"]


def test_audit_ambiguous_device_with_unmatched_device_flag_skips(sandbox_repo: Path, tmp_path: Path):
    output_dir = tmp_path / "reports"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("rules:\n  enabled: false\nlocal:\n  enabled: true\n  model: some/model\n")

    with patch("codecheck.cli.resolve_model_location", return_value=_ambiguous_location()):
        result = runner.invoke(
            app,
            [
                "audit", "--repo-path", str(sandbox_repo),
                "--config", str(config_path), "--output-dir", str(output_dir),
                "--device", "Nonexistent-Device",
            ],
        )

    assert result.exit_code == 0
    assert "does not match any candidate" in result.stdout
    report = json.loads((output_dir / "report.json").read_text())
    assert "local_llm" not in report["tiers_run"]


def test_audit_ambiguous_device_with_force_local_no_device_proceeds_unset(
    sandbox_repo: Path, tmp_path: Path
):
    output_dir = tmp_path / "reports"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "rules:\n  enabled: false\nlocal:\n  enabled: true\n  model: some/model\n"
        "  base_url: http://127.0.0.1:19999/v1/chat/completions\n"
    )

    with patch("codecheck.cli.resolve_model_location", return_value=_ambiguous_location()), \
         patch("codecheck.cli.set_preferred_device") as mock_set:
        result = runner.invoke(
            app,
            [
                "audit", "--repo-path", str(sandbox_repo),
                "--config", str(config_path), "--output-dir", str(output_dir),
                "--force-local",
            ],
        )

    mock_set.assert_not_called()
    assert result.exit_code == 0
    report = json.loads((output_dir / "report.json").read_text())
    assert "local_llm" in report["tiers_run"]


def test_diff_clean_change_exits_zero(sandbox_repo: Path, tmp_path: Path):
    _git(sandbox_repo, "checkout", "-q", "-b", "feature")
    (sandbox_repo / "clean.py").write_text("x = 1\n")
    _git(sandbox_repo, "add", "-A")
    _git(sandbox_repo, "commit", "-q", "-m", "clean change")

    output_dir = tmp_path / "reports"
    result = runner.invoke(
        app,
        [
            "diff", "--repo-path", str(sandbox_repo), "--base-ref", "main",
            "--output-dir", str(output_dir),
        ],
    )

    assert result.exit_code == 0
    assert "No findings" in result.stdout


def test_audit_cloud_cap_blocks_without_force(sandbox_repo: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    for i in range(60):
        (sandbox_repo / f"f{i}.py").write_text(f"x = {i}\n")
    _git(sandbox_repo, "add", "-A")
    _git(sandbox_repo, "commit", "-q", "-m", "many files")

    output_dir = tmp_path / "reports"
    result = runner.invoke(
        app,
        [
            "audit", "--repo-path", str(sandbox_repo), "--cloud",
            "--output-dir", str(output_dir),
        ],
    )

    assert result.exit_code == 2
    assert "Refusing to run the cloud tier" in result.stdout
    assert not output_dir.exists()


def test_audit_no_files_exits_zero(tmp_path: Path):
    repo = tmp_path / "empty_repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    result = runner.invoke(app, ["audit", "--repo-path", str(repo)])
    assert result.exit_code == 0
    assert "No files found" in result.stdout


def test_diff_staged_and_pr_are_mutually_exclusive(tmp_path: Path):
    result = runner.invoke(
        app, ["diff", "--repo-path", str(tmp_path), "--staged", "--pr", "5"]
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stdout


@pytest.fixture
def fake_origin_and_clone(tmp_path: Path) -> Path:
    """A local bare repo standing in for a GitHub remote, with PR #7 open
    (adds a bare except + unsafe frappe.db.sql call) against main.
    """
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "remote", "add", "origin", str(bare))

    (work / "a.py").write_text("x = 1\n")
    _git(work, "add", "a.py")
    _git(work, "commit", "-q", "-m", "initial")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-q", "origin", "main")
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")

    _git(work, "checkout", "-q", "-b", "feature")
    (work / "risky.py").write_text(
        "import frappe\n\n"
        "def get_thing(name):\n"
        "    try:\n"
        '        return frappe.db.sql(f"select * from tabThing where name = {name}")\n'
        "    except:\n"
        "        return None\n"
    )
    _git(work, "add", "risky.py")
    _git(work, "commit", "-q", "-m", "pr change")
    pr_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, check=True, capture_output=True, text=True
    ).stdout.strip()
    _git(work, "push", "-q", "origin", "feature")
    _git(work, "checkout", "-q", "main")
    subprocess.run(
        ["git", "update-ref", "refs/pull/7/head", pr_sha], cwd=bare, check=True, capture_output=True
    )

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(bare), str(clone))
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    return clone


def test_diff_pr_reviews_fetched_pr_and_leaves_repo_on_main(fake_origin_and_clone: Path, tmp_path: Path):
    clone = fake_origin_and_clone
    output_dir = tmp_path / "reports"

    result = runner.invoke(
        app,
        [
            "diff", "--repo-path", str(clone), "--pr", "7", "--base-ref", "main",
            "--output-dir", str(output_dir),
        ],
    )

    assert result.exit_code == 1  # RULE-002 is HIGH
    assert "RULE-002" in result.stdout
    assert (output_dir / "report.json").exists()

    # the user's own clone must never be switched off its current branch
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=clone, capture_output=True, text=True
    ).stdout.strip()
    assert branch == "main"
    assert not (clone / "risky.py").exists()


def test_diff_pr_url_conflicting_with_explicit_repo_url_errors(tmp_path: Path):
    result = runner.invoke(
        app,
        [
            "diff",
            "--repo-url", "https://github.com/other/repo",
            "--pr", "https://github.com/org/repo/pull/123",
        ],
    )
    assert result.exit_code == 2
    assert "don't match" in result.stdout


def test_diff_pr_invalid_value_errors(tmp_path: Path):
    result = runner.invoke(app, ["diff", "--repo-path", str(tmp_path), "--pr", "not-a-number-or-url"])
    assert result.exit_code == 2
    assert "PR number or a full PR URL" in result.stdout


def test_audit_repo_url_clones_and_reviews(fake_origin_and_clone: Path, tmp_path: Path):
    bare = fake_origin_and_clone.parent / "origin.git"
    output_dir = tmp_path / "reports"

    result = runner.invoke(
        app, ["audit", "--repo-url", str(bare), "--output-dir", str(output_dir)]
    )

    assert result.exit_code == 0
    assert "a.py" in result.stdout or "No findings" in result.stdout
    assert (output_dir / "report.json").exists()


class _FakeReviewer:
    """Duck-types the Reviewer interface (.review(), .skipped_files) so
    _run_llm_tier's resume wiring can be tested without a real HTTP client.
    """

    def __init__(self):
        self.skipped_files: list[tuple[str, str]] = []
        self.received_targets: list[ReviewTarget] | None = None

    def review(self, targets, repo_path, on_progress=None):
        self.received_targets = targets
        if on_progress:
            for t in targets:
                on_progress(t.path, "0 findings")
        return []  # no new findings this run -- only resumed ones matter here


def test_run_llm_tier_skips_already_succeeded_files_and_reuses_their_findings(tmp_path: Path):
    prior_report = {
        "tiers_run": ["cloud_llm"],
        "files_reviewed": ["a.py", "b.py"],
        "skipped": [],  # neither file was skipped last time -- both succeeded
        "findings": [
            {
                "check_id": "CLOUD-001",
                "tier": "cloud_llm",
                "source": "cloud_llm",
                "severity": "high",
                "title": "Prior finding",
                "explanation": "",
                "file": "a.py",
                "line_start": 1,
            },
        ],
    }
    targets = [
        ReviewTarget(path="a.py", status="scanned"),
        ReviewTarget(path="b.py", status="scanned"),
    ]
    reviewer = _FakeReviewer()

    findings, skip_entries, resumed = _run_llm_tier(
        "cloud_llm", reviewer, targets, tmp_path, prior_report, "status"
    )

    # both a.py and b.py were already done -- reviewer.review() must not be
    # asked to process either of them again
    assert reviewer.received_targets == []
    assert resumed == 2
    assert [f.check_id for f in findings] == ["CLOUD-001"]


def test_run_llm_tier_retries_only_files_skipped_last_time(tmp_path: Path):
    prior_report = {
        "tiers_run": ["cloud_llm"],
        "files_reviewed": ["a.py", "b.py"],
        "skipped": ["cloud_llm: b.py: API request failed: 429 Too Many Requests"],
        "findings": [],
    }
    targets = [
        ReviewTarget(path="a.py", status="scanned"),
        ReviewTarget(path="b.py", status="scanned"),
    ]
    reviewer = _FakeReviewer()

    findings, skip_entries, resumed = _run_llm_tier(
        "cloud_llm", reviewer, targets, tmp_path, prior_report, "status"
    )

    # a.py already succeeded -- only b.py (skipped last time) is re-requested
    assert [t.path for t in reviewer.received_targets] == ["b.py"]
    assert resumed == 1


def test_run_llm_tier_processes_everything_without_resume(tmp_path: Path):
    targets = [ReviewTarget(path="a.py", status="scanned")]
    reviewer = _FakeReviewer()

    findings, skip_entries, resumed = _run_llm_tier(
        "cloud_llm", reviewer, targets, tmp_path, None, "status"
    )

    assert [t.path for t in reviewer.received_targets] == ["a.py"]
    assert resumed == 0


def test_version_flag_prints_version_and_exits():
    from importlib.metadata import version

    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "codecheck" in result.stdout
    assert version("codecheck") in result.stdout
