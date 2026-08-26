from datetime import datetime, timezone

from codecheck.models import Finding, ReviewReport, Severity
from codecheck.redact import redact_report


def make_report(repo_path: str, skipped: list[str]) -> ReviewReport:
    findings = [
        Finding(
            check_id="RULE-002", tier="rules", source="house", severity=Severity.HIGH,
            title="frappe.db.sql() built with string formatting",
            explanation="SQL injection risk.", file="app/api.py", line_start=42, line_end=42,
        ),
    ]
    return ReviewReport(
        repo_path=repo_path, mode="diff", base_ref="main", head_ref="feature",
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        tiers_run=["rules"], findings=findings, files_reviewed=["app/api.py"],
        skipped=skipped,
    )


def test_redact_replaces_repo_path_with_placeholder_plus_repo_name():
    report = make_report("/Users/afshan/Workspace/personal-projects/codecheck", [])
    redacted = redact_report(report)
    assert "afshan" not in redacted.repo_path
    assert "codecheck" in redacted.repo_path  # repo name itself is fine to keep
    assert "<local repo>" in redacted.repo_path


def test_redact_leaves_original_report_untouched():
    report = make_report("/Users/afshan/Workspace/codecheck", [])
    redact_report(report)
    assert report.repo_path == "/Users/afshan/Workspace/codecheck"  # not mutated


def test_redact_scrubs_absolute_paths_out_of_skipped_entries():
    report = make_report(
        "/Users/afshan/Workspace/codecheck",
        ["rules: eslint: config error at /Users/afshan/Workspace/codecheck/.eslintrc.json"],
    )
    redacted = redact_report(report)
    assert "afshan" not in redacted.skipped[0]
    assert "<local repo>" in redacted.skipped[0]
    assert "config error at" in redacted.skipped[0]  # rest of the message survives


def test_redact_leaves_relative_paths_in_skipped_entries_alone():
    report = make_report(
        "/Users/afshan/Workspace/codecheck",
        ["local_llm: app/api.py: file too large (2100 lines > 2000)"],
    )
    redacted = redact_report(report)
    assert redacted.skipped[0] == report.skipped[0]  # nothing absolute to scrub here


def test_redact_does_not_touch_findings():
    report = make_report("/Users/afshan/Workspace/codecheck", [])
    redacted = redact_report(report)
    assert redacted.findings == report.findings
