import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codecheck.cli import (
    _claim_unique_run_dir,
    _display_repo_url,
    _finish,
    _repo_label,
    _report_basename,
    _run_llm_tier,
    app,
)
from codecheck.config import Config
from codecheck.lm_link import DeviceCandidate, ModelLocation
from codecheck.models import ReviewReport, ReviewTarget

runner = CliRunner()


def _report_json_path(output_dir: Path) -> Path:
    matches = list(output_dir.glob("*/*.json"))
    assert len(matches) == 1, f"expected exactly one .json report, found {matches}"
    return matches[0]


def _report_md_path(output_dir: Path) -> Path:
    matches = list(output_dir.glob("*/*.md"))
    assert len(matches) == 1, f"expected exactly one .md report, found {matches}"
    return matches[0]


def _report_docx_path(output_dir: Path) -> Path:
    matches = list(output_dir.glob("*/*.docx"))
    assert len(matches) == 1, f"expected exactly one .docx report, found {matches}"
    return matches[0]


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


def test_repo_label_from_repo_url_https():
    assert _repo_label(Path("."), "https://github.com/AfshanKhan/codecheck") == "AfshanKhan_codecheck"


def test_repo_label_from_repo_url_with_git_suffix():
    assert _repo_label(Path("."), "https://github.com/org/repo.git") == "org_repo"


def test_repo_label_from_repo_url_ssh():
    assert _repo_label(Path("."), "git@github.com:org/repo.git") == "org_repo"


def test_repo_label_falls_back_to_local_dir_name(sandbox_repo: Path):
    assert _repo_label(sandbox_repo, None) == "sandbox"


def test_display_repo_url_strips_embedded_credentials():
    # regression (CodeRabbit): _validate_clone_url doesn't reject credentials
    # in an HTTPS URL, and a URL-shaped repo_path is left unredacted by
    # --redact (nothing local-identifying about a plain remote URL) -- so a
    # credential embedded in --repo-url would otherwise reach every report
    # format unredacted.
    assert (
        _display_repo_url("https://user:secretpass@github.com/org/repo.git")
        == "https://github.com/org/repo.git"
    )


def test_display_repo_url_leaves_ssh_scp_syntax_untouched():
    # git@host:org/repo.git has no userinfo component to strip (no "://", so
    # urlsplit never treats "git@host" as netloc) -- its "git@" is the SSH
    # transport user, not a secret.
    assert _display_repo_url("git@github.com:org/repo.git") == "git@github.com:org/repo.git"


def test_display_repo_url_leaves_credential_free_urls_untouched():
    assert (
        _display_repo_url("https://github.com/org/repo.git")
        == "https://github.com/org/repo.git"
    )


def test_display_repo_url_strips_token_in_query_string():
    # regression (CodeRabbit): a token can arrive as a query parameter
    # instead of userinfo, e.g. GitHub's own
    # https://github.com/org/repo.git?access_token=... pattern -- the
    # original fix only stripped query/fragment when userinfo was ALSO
    # present, so a query-only token slipped through untouched.
    assert (
        _display_repo_url("https://github.com/org/repo.git?access_token=secret")
        == "https://github.com/org/repo.git"
    )


def test_display_repo_url_strips_credentials_query_and_fragment_together():
    assert (
        _display_repo_url("https://user:pass@github.com/org/repo.git?token=secret#frag")
        == "https://github.com/org/repo.git"
    )


def test_report_basename_uses_pr_number_when_present():
    generated_at = datetime(2026, 8, 14, 16, 10, 0, tzinfo=timezone.utc)
    assert _report_basename("org_repo", 12, "diff", generated_at) == "org_repo_pr12_20260814_161000"


def test_report_basename_uses_mode_when_no_pr():
    generated_at = datetime(2026, 8, 14, 16, 10, 0, tzinfo=timezone.utc)
    assert _report_basename("myrepo", None, "audit", generated_at) == "myrepo_audit_20260814_161000"


def test_claim_unique_run_dir_no_collision_claims_as_is(tmp_path: Path):
    run_dir = _claim_unique_run_dir(tmp_path, "myrepo_audit_20260814_161000")
    assert run_dir == tmp_path / "myrepo_audit_20260814_161000"
    assert run_dir.is_dir()  # claimed as a side effect


def test_claim_unique_run_dir_appends_suffix_on_collision(tmp_path: Path):
    # regression (Greptile, adapted): two runs finishing within the same
    # second must not silently overwrite each other's reports -- now
    # expressed as one run's whole directory colliding with another's,
    # rather than four separate per-extension file collisions.
    (tmp_path / "myrepo_audit_20260814_161000").mkdir()
    run_dir = _claim_unique_run_dir(tmp_path, "myrepo_audit_20260814_161000")
    assert run_dir == tmp_path / "myrepo_audit_20260814_161000-2"

    # both the original name and its first "-2" suffix are now taken (the
    # base by the line above, "-2" as a side effect of the claim itself) --
    # a third claim for the same basename must skip both and land on "-3"
    run_dir = _claim_unique_run_dir(tmp_path, "myrepo_audit_20260814_161000")
    assert run_dir == tmp_path / "myrepo_audit_20260814_161000-3"


def test_claim_unique_run_dir_is_atomic_not_toctou(tmp_path: Path):
    # regression (Greptile, adapted): two "concurrent" claims for the same
    # basename must never both succeed with the same directory -- simulated
    # here by claiming twice in a row without an intervening write, which a
    # check-then-write implementation would incorrectly allow.
    first = _claim_unique_run_dir(tmp_path, "myrepo_audit_20260814_161000")
    second = _claim_unique_run_dir(tmp_path, "myrepo_audit_20260814_161000")
    assert first != second


def test_claim_unique_run_dir_does_not_touch_an_unrelated_existing_directory(tmp_path: Path):
    other = tmp_path / "myrepo_audit_20260814_161000"
    other.mkdir()
    (other / "existing-report.json").write_text("existing report, don't touch")
    run_dir = _claim_unique_run_dir(tmp_path, "myrepo_audit_20260814_161000")
    assert run_dir == tmp_path / "myrepo_audit_20260814_161000-2"
    assert (other / "existing-report.json").read_text() == "existing report, don't touch"


def test_claim_unique_run_dir_reraises_non_collision_os_error(tmp_path: Path, monkeypatch):
    # regression (Greptile, adapted): a non-FileExistsError OSError
    # (permission denied, disk full, ...) must propagate rather than being
    # swallowed and silently retried against the next suffix -- an error
    # like that will likely recur identically for every candidate.
    def flaky_mkdir(self, *args, **kwargs):
        raise PermissionError("simulated I/O error")

    monkeypatch.setattr(Path, "mkdir", flaky_mkdir)

    with pytest.raises(PermissionError):
        _claim_unique_run_dir(tmp_path, "myrepo_audit_20260814_161000")


def test_finish_cleans_up_claimed_files_when_a_reporter_raises(tmp_path: Path, monkeypatch):
    # regression (Greptile): a reporter raising after the basename was claimed
    # would otherwise leave that basename permanently poisoned by empty/partial
    # files -- no future run could ever reuse it, and it'd look like a real
    # (but empty) report to anyone who opened it.
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("codecheck.cli.write_markdown_report", _boom)

    report = ReviewReport(
        repo_path="/repo",
        mode="audit",
        base_ref=None,
        head_ref=None,
        generated_at=datetime(2026, 8, 14, 16, 10, 0, tzinfo=timezone.utc),
        tiers_run=["rules"],
    )

    with pytest.raises(RuntimeError, match="boom"):
        _finish(report, tmp_path, Config(), "myrepo")

    remaining = list(tmp_path.iterdir())
    assert remaining == [], f"expected no leftover files, found {remaining}"


def test_audit_finds_house_rule_violations(sandbox_repo: Path, tmp_path: Path):
    output_dir = tmp_path / "reports"
    result = runner.invoke(
        app, ["audit", "--repo-path", str(sandbox_repo), "--output-dir", str(output_dir)]
    )

    assert result.exit_code == 1  # RULE-002 is HIGH, hits the default fail_on_severity
    assert "RULE-002" in result.stdout
    json_path = _report_json_path(output_dir)
    assert json_path.exists()
    assert json_path.name.startswith("sandbox_audit_")
    assert _report_md_path(output_dir).exists()
    assert _report_docx_path(output_dir).exists()


def test_audit_local_without_model_configured_skips_gracefully(sandbox_repo: Path, tmp_path: Path):
    output_dir = tmp_path / "reports"
    result = runner.invoke(
        app, ["audit", "--repo-path", str(sandbox_repo), "--local", "--output-dir", str(output_dir)]
    )

    assert result.exit_code == 1  # rules tier still finds RULE-002
    assert "local.model not set" in result.stdout
    assert "LOCAL-" not in result.stdout

    report = json.loads(_report_json_path(output_dir).read_text())
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
    report = json.loads(_report_json_path(output_dir).read_text())
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
    report = json.loads(_report_json_path(output_dir).read_text())
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
    report = json.loads(_report_json_path(output_dir).read_text())
    # confirmed remote -> tier actually ran (and then failed per-file on the
    # unreachable port, which is a skip-not-crash, not a tier-level skip)
    assert "local_llm" in report["tiers_run"]
    assert any("API request failed" in s for s in report["skipped"])
    assert _report_json_path(output_dir).exists()


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
    report = json.loads(_report_json_path(output_dir).read_text())
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
    report = json.loads(_report_json_path(output_dir).read_text())
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
    report = json.loads(_report_json_path(output_dir).read_text())
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
    report = json.loads(_report_json_path(output_dir).read_text())
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
    json_path = _report_json_path(output_dir)
    assert json_path.exists()
    assert "_pr7_" in json_path.name  # naming convention: <repo>_pr<N>_<timestamp>.json

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
    assert _report_json_path(output_dir).exists()


def test_audit_repo_url_report_shows_the_url_not_the_local_clone_path(
    fake_origin_and_clone: Path, tmp_path: Path
):
    # regression: repo_path used to always be the local temp clone's own
    # directory (codecheck-clone-xxxxx), meaningless to anyone reading the
    # report -- it should be the --repo-url the user actually passed.
    bare = fake_origin_and_clone.parent / "origin.git"
    output_dir = tmp_path / "reports"

    result = runner.invoke(
        app, ["audit", "--repo-url", str(bare), "--output-dir", str(output_dir)]
    )
    assert result.exit_code == 0
    data = json.loads(_report_json_path(output_dir).read_text())
    assert data["repo_path"] == str(bare)
    assert "codecheck-clone" not in data["repo_path"]


def test_audit_local_repo_path_report_shows_the_local_path(sandbox_repo: Path, tmp_path: Path):
    output_dir = tmp_path / "reports"
    runner.invoke(app, ["audit", "--repo-path", str(sandbox_repo), "--output-dir", str(output_dir)])
    data = json.loads(_report_json_path(output_dir).read_text())
    assert data["repo_path"] == str(sandbox_repo.resolve())


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


_HASHES = {"a.py": "hash-a", "b.py": "hash-b"}


def test_run_llm_tier_skips_already_succeeded_files_and_reuses_their_findings(tmp_path: Path):
    prior_report = {
        "mode": "audit",
        "tiers_run": ["cloud_llm"],
        "files_reviewed": ["a.py", "b.py"],
        "skipped": [],  # neither file was skipped last time -- both succeeded
        "file_hashes": dict(_HASHES),
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
        "cloud_llm", reviewer, targets, tmp_path, prior_report, "audit", _HASHES, "status"
    )

    # both a.py and b.py were already done -- reviewer.review() must not be
    # asked to process either of them again
    assert reviewer.received_targets == []
    assert resumed == 2
    assert [f.check_id for f in findings] == ["CLOUD-001"]


def test_run_llm_tier_retries_only_files_skipped_last_time(tmp_path: Path):
    prior_report = {
        "mode": "audit",
        "tiers_run": ["cloud_llm"],
        "files_reviewed": ["a.py", "b.py"],
        "skipped": ["cloud_llm: b.py: API request failed: 429 Too Many Requests"],
        "file_hashes": dict(_HASHES),
        "findings": [],
    }
    targets = [
        ReviewTarget(path="a.py", status="scanned"),
        ReviewTarget(path="b.py", status="scanned"),
    ]
    reviewer = _FakeReviewer()

    findings, skip_entries, resumed = _run_llm_tier(
        "cloud_llm", reviewer, targets, tmp_path, prior_report, "audit", _HASHES, "status"
    )

    # a.py already succeeded -- only b.py (skipped last time) is re-requested
    assert [t.path for t in reviewer.received_targets] == ["b.py"]
    assert resumed == 1


def test_run_llm_tier_processes_everything_without_resume(tmp_path: Path):
    targets = [ReviewTarget(path="a.py", status="scanned")]
    reviewer = _FakeReviewer()

    findings, skip_entries, resumed = _run_llm_tier(
        "cloud_llm", reviewer, targets, tmp_path, None, "audit", {"a.py": "hash-a"}, "status"
    )

    assert [t.path for t in reviewer.received_targets] == ["a.py"]
    assert resumed == 0


def test_version_flag_prints_version_and_exits():
    from importlib.metadata import version

    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "codecheck" in result.stdout
    assert version("codecheck") in result.stdout


# --- --gate / --redact / render / compare / --suggest-fixes -----------------


def test_gate_relaxed_downgrades_a_high_finding_to_not_failing(sandbox_repo: Path, tmp_path: Path):
    output_dir = tmp_path / "reports"
    result = runner.invoke(
        app,
        [
            "audit", "--repo-path", str(sandbox_repo), "--output-dir", str(output_dir),
            "--gate", "relaxed",
        ],
    )
    # RULE-002 is HIGH; --gate relaxed only fails on CRITICAL -- same findings,
    # different exit code than the default run.
    assert result.exit_code == 0
    assert "RULE-002" in result.stdout


def test_gate_strict_still_fails_on_a_high_finding(sandbox_repo: Path, tmp_path: Path):
    output_dir = tmp_path / "reports"
    result = runner.invoke(
        app,
        [
            "audit", "--repo-path", str(sandbox_repo), "--output-dir", str(output_dir),
            "--gate", "strict",
        ],
    )
    assert result.exit_code == 1


def test_gate_rejects_an_unknown_profile_name(sandbox_repo: Path, tmp_path: Path):
    output_dir = tmp_path / "reports"
    result = runner.invoke(
        app,
        [
            "audit", "--repo-path", str(sandbox_repo), "--output-dir", str(output_dir),
            "--gate", "not-a-real-profile",
        ],
    )
    assert result.exit_code == 2
    assert "--gate" in result.stdout


def test_redact_scrubs_repo_path_from_written_json(sandbox_repo: Path, tmp_path: Path):
    output_dir = tmp_path / "reports"
    result = runner.invoke(
        app,
        ["audit", "--repo-path", str(sandbox_repo), "--output-dir", str(output_dir), "--redact"],
    )
    assert result.exit_code == 1  # findings are unaffected by --redact
    json_path = _report_json_path(output_dir)
    data = json.loads(json_path.read_text())
    assert str(sandbox_repo) not in data["repo_path"]
    assert "<local repo>" in data["repo_path"]
    # the terminal output (this process's own machine) is not redacted, only
    # the written files -- the sandbox repo's real path is fine to show here
    assert "RULE-002" in result.stdout


def test_render_recreates_reports_from_a_prior_json_without_rerunning_checks(
    sandbox_repo: Path, tmp_path: Path
):
    first_output = tmp_path / "reports1"
    runner.invoke(app, ["audit", "--repo-path", str(sandbox_repo), "--output-dir", str(first_output)])
    json_path = _report_json_path(first_output)

    second_output = tmp_path / "reports2"
    result = runner.invoke(app, ["render", str(json_path), "--output-dir", str(second_output)])

    assert result.exit_code == 0
    rendered_json = _report_json_path(second_output)
    rendered_md = _report_md_path(second_output)
    rendered_docx = _report_docx_path(second_output)
    assert rendered_json.exists()
    assert "RULE-002" in rendered_md.read_text()
    assert rendered_docx.exists()


def test_render_with_redact_scrubs_the_repo_path(sandbox_repo: Path, tmp_path: Path):
    first_output = tmp_path / "reports1"
    runner.invoke(app, ["audit", "--repo-path", str(sandbox_repo), "--output-dir", str(first_output)])
    json_path = _report_json_path(first_output)

    second_output = tmp_path / "reports2"
    runner.invoke(app, ["render", str(json_path), "--output-dir", str(second_output), "--redact"])

    rendered_json = _report_json_path(second_output)
    data = json.loads(rendered_json.read_text())
    assert str(sandbox_repo) not in data["repo_path"]


def test_render_rejects_a_nonexistent_report_path(tmp_path: Path):
    result = runner.invoke(app, ["render", str(tmp_path / "does_not_exist.json")])
    assert result.exit_code == 2


def test_render_rejects_malformed_json(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    result = runner.invoke(app, ["render", str(bad)])
    assert result.exit_code == 2


def _write_report_json(path: Path, findings: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {
                "repo_path": "/repo",
                "mode": "audit",
                "base_ref": None,
                "head_ref": None,
                "generated_at": "2026-08-20T10:00:00+00:00",
                "tiers_run": ["rules"],
                "duration_seconds": 1.0,
                "files_reviewed": [],
                "skipped": [],
                "file_hashes": {},
                "counts_by_severity": {},
                "findings": findings,
            }
        )
    )


def _finding_dict(check_id: str, file: str, line: int, severity: str = "medium") -> dict:
    return {
        "check_id": check_id, "tier": "rules", "source": "house", "severity": severity,
        "title": f"{check_id} finding", "explanation": "explanation", "file": file,
        "line_start": line, "line_end": line, "suggestion": None, "raw": None,
    }


def test_compare_reports_added_and_resolved_findings(tmp_path: Path):
    old_path = tmp_path / "old.json"
    new_path = tmp_path / "new.json"
    _write_report_json(old_path, [_finding_dict("RULE-001", "a.py", 10)])
    _write_report_json(
        new_path,
        [_finding_dict("RULE-002", "b.py", 5, severity="high")],  # RULE-001 gone, RULE-002 new
    )

    result = runner.invoke(app, ["compare", str(old_path), str(new_path)])

    assert "1 newly introduced finding" in result.stdout
    assert "RULE-002" in result.stdout
    assert "1 resolved finding" in result.stdout
    assert "RULE-001" in result.stdout
    assert result.exit_code == 1  # RULE-002 is HIGH, at/above the default fail_on_severity


def test_compare_exit_code_0_when_no_new_finding_meets_the_gate(tmp_path: Path):
    old_path = tmp_path / "old.json"
    new_path = tmp_path / "new.json"
    _write_report_json(old_path, [])
    _write_report_json(new_path, [_finding_dict("RULE-007", "a.py", 1, severity="low")])

    result = runner.invoke(app, ["compare", str(old_path), str(new_path)])

    assert result.exit_code == 0  # LOW is below the default "high" gate


def test_compare_respects_gate_override(tmp_path: Path):
    old_path = tmp_path / "old.json"
    new_path = tmp_path / "new.json"
    _write_report_json(old_path, [])
    # MEDIUM is below the default "high" gate but at/above --gate strict ("medium")
    _write_report_json(new_path, [_finding_dict("RULE-007", "a.py", 1, severity="medium")])

    default_result = runner.invoke(app, ["compare", str(old_path), str(new_path)])
    assert default_result.exit_code == 0

    strict_result = runner.invoke(app, ["compare", str(old_path), str(new_path), "--gate", "strict"])
    assert strict_result.exit_code == 1


def test_compare_no_changes_between_identical_reports(tmp_path: Path):
    old_path = tmp_path / "old.json"
    new_path = tmp_path / "new.json"
    findings = [_finding_dict("RULE-001", "a.py", 10)]
    _write_report_json(old_path, findings)
    _write_report_json(new_path, findings)

    result = runner.invoke(app, ["compare", str(old_path), str(new_path)])

    assert result.exit_code == 0
    assert "No newly introduced findings" in result.stdout
    assert "No resolved findings" in result.stdout


def test_suggest_fixes_without_cloud_or_local_records_a_skip_not_a_crash(
    sandbox_repo: Path, tmp_path: Path
):
    output_dir = tmp_path / "reports"
    result = runner.invoke(
        app,
        [
            "audit", "--repo-path", str(sandbox_repo), "--output-dir", str(output_dir),
            "--suggest-fixes",
        ],
    )
    assert result.exit_code == 1  # the underlying findings are unaffected
    json_path = _report_json_path(output_dir)
    data = json.loads(json_path.read_text())
    assert any("suggest_fixes" in entry for entry in data["skipped"])


def test_maybe_suggest_fixes_with_default_anthropic_cloud_provider_does_not_crash(
    tmp_path: Path, monkeypatch
):
    # regression (Greptile): AnthropicCloudReviewer (cloud.provider defaults
    # to "anthropic") isn't an OpenAIProtocolReviewer -- it has no
    # _get_client()/_resolved_base_url(), so handing it to
    # generate_suggestions used to raise an uncaught AttributeError *after*
    # the review findings were already computed, losing the whole run instead
    # of just skipping the suggestion pass. Tested directly against
    # _maybe_suggest_fixes (not the full CLI) so this doesn't need a real
    # network call to verify -- a real ANTHROPIC_API_KEY would make
    # AnthropicCloudReviewer.is_available() return True, but the fix means it
    # must never even get that far.
    from codecheck.cli import _maybe_suggest_fixes

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    cfg = Config()
    cfg.cloud.enabled = True  # provider defaults to "anthropic"
    skipped: list[str] = []

    _maybe_suggest_fixes(True, cfg, [], [], tmp_path, skipped)  # must not raise

    assert any("suggest_fixes" in entry and "Anthropic" in entry for entry in skipped)


def test_maybe_suggest_fixes_falling_back_to_local_records_why(tmp_path: Path, monkeypatch):
    # regression (Greptile): when cloud is enabled with an unsupported
    # provider (Anthropic) and local is also enabled and available,
    # _maybe_suggest_fixes used to fall through to local silently -- nothing
    # in the report said cloud was configured but skipped, even though the
    # docs describe cloud as preferred whenever both are on. Now the fallback
    # itself is recorded as a skipped entry.
    import codecheck.cli as cli_module
    from codecheck.cli import _maybe_suggest_fixes

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setattr(
        cli_module.LocalLLMReviewer, "is_available", lambda self, repo_path: (True, None)
    )
    monkeypatch.setattr(cli_module, "generate_suggestions", lambda *a, **k: (0, []))
    cfg = Config()
    cfg.cloud.enabled = True  # provider defaults to "anthropic" -- unsupported here
    cfg.local.enabled = True
    cfg.local.model = "some-model"
    skipped: list[str] = []

    _maybe_suggest_fixes(True, cfg, [], [], tmp_path, skipped)

    assert any(
        "suggest_fixes" in entry and "falling back to --local" in entry for entry in skipped
    )
