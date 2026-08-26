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


def test_redact_scrubs_absolute_path_containing_a_space_in_a_component():
    # regression (Greptile, security): the original regex restricted each
    # path segment to [\w.\-]+ (no spaces), so a real, common macOS/Windows
    # path like "/Users/John Doe/project" only had its prefix redacted --
    # "/Users" got replaced but "/John Doe/project" (the actual identifying
    # part, often the real username) survived untouched in the report.
    report = make_report(
        "/Users/afshan/Workspace/codecheck",
        ["rules: semgrep: config error at /Users/John Doe/some project/app.js"],
    )
    redacted = redact_report(report)
    assert "John Doe" not in redacted.skipped[0]
    assert "some project" not in redacted.skipped[0]
    assert "<local repo>" in redacted.skipped[0]


def test_redact_scrubs_spaced_path_and_stops_at_a_clear_terminator():
    report = make_report(
        "/Users/afshan/Workspace/codecheck",
        ["rules: semgrep: at /Users/John Doe/project/app.py: line 5, see the docs"],
    )
    redacted = redact_report(report)
    assert "John Doe" not in redacted.skipped[0]
    assert "line 5, see the docs" in redacted.skipped[0]  # trailing prose after ":" survives


def test_redact_scrubs_absolute_path_with_apostrophe_parenthesis_and_plus_sign():
    # regression (Greptile, security): the space-widened char class was still
    # a whitelist ([\w.\- ]), so any other valid filesystem character --
    # apostrophe, parenthesis, plus sign, etc. -- stopped the match early and
    # leaked the identifying suffix, same failure mode as the earlier
    # space-only gap.
    report = make_report(
        "/Users/afshan/Workspace/codecheck",
        ["rules: semgrep: config error at /Users/O'Brien's Files/project (v2)+final/app.js"],
    )
    redacted = redact_report(report)
    assert "O'Brien" not in redacted.skipped[0]
    assert "project (v2)+final" not in redacted.skipped[0]
    assert "<local repo>" in redacted.skipped[0]


def test_redact_leaves_relative_paths_in_skipped_entries_alone():
    report = make_report(
        "/Users/afshan/Workspace/codecheck",
        ["local_llm: app/api.py: file too large (2100 lines > 2000)"],
    )
    redacted = redact_report(report)
    assert redacted.skipped[0] == report.skipped[0]  # nothing absolute to scrub here


def test_redact_leaves_findings_without_absolute_paths_unchanged():
    report = make_report("/Users/afshan/Workspace/codecheck", [])
    redacted = redact_report(report)
    assert redacted.findings[0].title == report.findings[0].title
    assert redacted.findings[0].explanation == report.findings[0].explanation


def test_redact_preserves_filename_after_the_repo_root_is_scrubbed():
    # regression (found while verifying the raw-payload fix live): scrubbing
    # in two passes -- the exact repo_path first, then the general
    # any-absolute-path regex -- used to let the second pass re-match the
    # leftover "/app.py" remainder as if it were its own fresh absolute path,
    # turning "<local repo>/app.py" into "<local repo><local repo>" and
    # losing which file the raw payload was actually about.
    real_path = "/Users/afshan/Workspace/codecheck"
    finding = Finding(
        check_id="RUFF-F401", tier="rules", source="ruff", severity=Severity.LOW,
        title="F401", explanation="e", file="app/api.py", line_start=1,
        raw={"filename": f"{real_path}/app/api.py"},
    )
    report = ReviewReport(
        repo_path=real_path, mode="diff", base_ref="main", head_ref="feature",
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        tiers_run=["rules"], findings=[finding],
    )
    redacted = redact_report(report)
    assert redacted.findings[0].raw["filename"] == "<local repo>/app/api.py"


def test_redact_scrubs_absolute_path_out_of_finding_raw_payload():
    # regression (Greptile, security): ruff's own --output-format=json output
    # carries an absolute "filename" field, stored unchanged in Finding.raw --
    # RuffRunner relativizes the *displayed* Finding.file separately, but the
    # raw payload used to bypass redaction entirely, leaking the real path.
    real_path = "/Users/afshan/Workspace/codecheck"
    finding = Finding(
        check_id="RUFF-F401", tier="rules", source="ruff", severity=Severity.LOW,
        title="F401: unused import", explanation="Unused import 'os'.",
        file="app/api.py", line_start=1, line_end=1,
        raw={"filename": f"{real_path}/app/api.py", "code": "F401", "location": {"row": 1}},
    )
    report = ReviewReport(
        repo_path=real_path, mode="diff", base_ref="main", head_ref="feature",
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        tiers_run=["rules"], findings=[finding],
    )
    redacted = redact_report(report)
    assert "afshan" not in redacted.findings[0].raw["filename"]
    assert redacted.findings[0].raw["code"] == "F401"  # non-path fields untouched
    assert redacted.findings[0].raw["location"] == {"row": 1}  # nested non-string values survive


def test_redact_scrubs_absolute_path_out_of_finding_suggestion():
    real_path = "/Users/afshan/Workspace/codecheck"
    finding = Finding(
        check_id="RULE-002", tier="rules", source="house", severity=Severity.HIGH,
        title="t", explanation="e", file="app/api.py", line_start=1,
        suggestion=f"See {real_path}/app/api.py for the parameterized version.",
    )
    report = ReviewReport(
        repo_path=real_path, mode="diff", base_ref="main", head_ref="feature",
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        tiers_run=["rules"], findings=[finding],
    )
    redacted = redact_report(report)
    assert "afshan" not in redacted.findings[0].suggestion


def test_redact_does_not_mutate_the_original_findings():
    report = make_report("/Users/afshan/Workspace/codecheck", [])
    redact_report(report)
    assert report.findings[0].raw is None  # unchanged
