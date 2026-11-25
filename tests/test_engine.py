from pathlib import Path

from codecheck.config import Config
from codecheck.engine import AuditCapExceededError, ReviewEngine, ReviewRequest
from codecheck.models import ReviewTarget
from codecheck.source import InMemorySource


def test_engine_empty_targets():
    engine = ReviewEngine()
    source = InMemorySource(targets=[], repo_path=Path.cwd())
    with source.acquire() as ws:
        req = ReviewRequest(workspace=ws, config=Config())
        result = engine.review(req)
        assert result.report.files_reviewed == []
        assert result.had_runner_error is False
        assert result.gate_passed is True


def test_engine_runs_rules_tier_on_python_file(tmp_path: Path):
    py_file = tmp_path / "bad.py"
    py_file.write_text("try:\n    pass\nexcept:\n    pass\n")

    target = ReviewTarget(path="bad.py", status="modified", diff_text="", changed_lines={3})
    source = InMemorySource(targets=[target], repo_path=tmp_path)

    engine = ReviewEngine()
    with source.acquire() as ws:
        cfg = Config()
        req = ReviewRequest(workspace=ws, config=cfg)
        result = engine.review(req)

        assert "rules" in result.report.tiers_run
        assert any(f.check_id == "RULE-001" for f in result.report.findings)
        assert result.had_runner_error is False


def test_engine_audit_cap_exceeded(tmp_path: Path):
    targets = [ReviewTarget(path=f"f{i}.py", status="added", diff_text="", changed_lines={1}) for i in range(15)]
    for t in targets:
        (tmp_path / t.path).write_text("x = 1\n")

    source = InMemorySource(targets=targets, repo_path=tmp_path)
    engine = ReviewEngine()
    with source.acquire() as ws:
        cfg = Config()
        cfg.cloud.enabled = True
        cfg.cloud.audit_file_cap = 10
        req = ReviewRequest(workspace=ws, config=cfg, force_cloud=False)

        try:
            engine.review(req)
            assert False, "Should have raised AuditCapExceededError"
        except AuditCapExceededError as e:
            assert e.count == 15
            assert e.cap == 10


def test_engine_all_tiers_disabled_fails_gate(tmp_path: Path):
    py_file = tmp_path / "ok.py"
    py_file.write_text("x = 1\n")

    target = ReviewTarget(path="ok.py", status="modified", diff_text="", changed_lines={1})
    source = InMemorySource(targets=[target], repo_path=tmp_path)

    cfg = Config()
    cfg.rules.enabled = False
    cfg.local.enabled = False
    cfg.cloud.enabled = False

    engine = ReviewEngine()
    with source.acquire() as ws:
        result = engine.review(ReviewRequest(workspace=ws, config=cfg))
        assert result.had_runner_error is True
        assert result.gate_passed is False
