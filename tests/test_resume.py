import json
from pathlib import Path

from codecheck.resume import already_succeeded_paths, load_prior_report, prior_findings_for_paths


def _write_report(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(data))
    return path


def _sample_report() -> dict:
    return {
        "tiers_run": ["rules", "cloud_llm"],
        "files_reviewed": ["a.py", "b.py", "c.py"],
        "skipped": [
            "rules: eslint: no eslint config found in repo",
            "cloud_llm: c.py: API request failed: 429 Too Many Requests",
        ],
        "findings": [
            {
                "check_id": "CLOUD-001",
                "tier": "cloud_llm",
                "source": "cloud_llm",
                "severity": "high",
                "title": "Some finding",
                "explanation": "explanation",
                "file": "a.py",
                "line_start": 5,
                "line_end": 5,
                "suggestion": None,
                "raw": {"severity": "high"},
            },
        ],
    }


def test_load_prior_report_returns_none_for_missing_file(tmp_path: Path):
    assert load_prior_report(tmp_path / "does-not-exist.json") is None


def test_load_prior_report_returns_none_for_malformed_json(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    assert load_prior_report(path) is None


def test_load_prior_report_parses_valid_json(tmp_path: Path):
    path = _write_report(tmp_path, _sample_report())
    assert load_prior_report(path)["tiers_run"] == ["rules", "cloud_llm"]


def test_already_succeeded_paths_excludes_files_skipped_by_that_tier():
    report = _sample_report()
    # a.py and b.py succeeded (no cloud_llm skip entry for them); c.py was
    # rate-limited last time and should be retried, not treated as done.
    assert already_succeeded_paths(report, "cloud_llm") == {"a.py", "b.py"}


def test_already_succeeded_paths_empty_when_tier_never_ran():
    report = _sample_report()
    assert already_succeeded_paths(report, "local_llm") == set()


def test_already_succeeded_paths_handles_malformed_report_gracefully():
    # regression: a hand-edited or corrupted --resume-from file must not crash
    # the CLI -- fail closed (treat as "nothing resumable") instead.
    assert already_succeeded_paths({}, "cloud_llm") == set()
    assert already_succeeded_paths({"tiers_run": "not-a-list"}, "cloud_llm") == set()
    assert already_succeeded_paths(
        {"tiers_run": ["cloud_llm"], "files_reviewed": "not-a-list", "skipped": []}, "cloud_llm"
    ) == set()


def test_prior_findings_for_paths_reconstructs_matching_findings():
    report = _sample_report()
    findings = prior_findings_for_paths(report, "cloud_llm", {"a.py"})
    assert len(findings) == 1
    assert findings[0].check_id == "CLOUD-001"
    assert findings[0].file == "a.py"
    assert findings[0].severity.value == "high"


def test_prior_findings_for_paths_excludes_other_tiers_and_paths():
    report = _sample_report()
    assert prior_findings_for_paths(report, "cloud_llm", {"b.py"}) == []
    assert prior_findings_for_paths(report, "local_llm", {"a.py"}) == []


def test_prior_findings_for_paths_skips_malformed_finding_entries():
    report = {
        "findings": [
            "not-a-dict",
            {"tier": "cloud_llm", "file": "a.py"},  # missing check_id
            {"tier": "cloud_llm", "check_id": "CLOUD-002", "file": "a.py", "severity": "low"},
        ]
    }
    findings = prior_findings_for_paths(report, "cloud_llm", {"a.py"})
    assert len(findings) == 1
    assert findings[0].check_id == "CLOUD-002"
