from datetime import datetime, timezone

from codecheck.models import Finding, ReviewReport, Severity
from codecheck.report_diff import diff_reports


def _report(findings: list[Finding]) -> ReviewReport:
    return ReviewReport(
        repo_path="/repo", mode="audit", base_ref=None, head_ref=None,
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        tiers_run=["rules"], findings=findings,
    )


def _finding(check_id: str, file: str, line: int, title: str = "title") -> Finding:
    return Finding(
        check_id=check_id, tier="rules", source="house", severity=Severity.MEDIUM,
        title=title, explanation="explanation", file=file, line_start=line,
    )


def test_no_changes_between_identical_reports():
    findings = [_finding("RULE-001", "a.py", 10)]
    added, resolved = diff_reports(_report(findings), _report(findings))
    assert added == []
    assert resolved == []


def test_new_finding_in_new_report_is_added():
    old = _report([])
    new = _report([_finding("RULE-001", "a.py", 10)])
    added, resolved = diff_reports(old, new)
    assert len(added) == 1
    assert added[0].check_id == "RULE-001"
    assert resolved == []


def test_finding_missing_from_new_report_is_resolved():
    old = _report([_finding("RULE-001", "a.py", 10)])
    new = _report([])
    added, resolved = diff_reports(old, new)
    assert added == []
    assert len(resolved) == 1
    assert resolved[0].check_id == "RULE-001"


def test_same_file_check_line_is_unchanged_even_if_title_wording_differs():
    # identity is (file, check_id, line_start) -- not exact title match, so a
    # rule's explanation text improving between codecheck versions doesn't
    # spuriously mark every finding as both resolved and newly added.
    old = _report([_finding("RULE-001", "a.py", 10, title="old wording")])
    new = _report([_finding("RULE-001", "a.py", 10, title="new wording")])
    added, resolved = diff_reports(old, new)
    assert added == []
    assert resolved == []


def test_mixed_added_and_resolved():
    old = _report([_finding("RULE-001", "a.py", 10), _finding("RULE-002", "b.py", 5)])
    new = _report([_finding("RULE-001", "a.py", 10), _finding("RULE-003", "c.py", 1)])
    added, resolved = diff_reports(old, new)
    assert [f.check_id for f in added] == ["RULE-003"]
    assert [f.check_id for f in resolved] == ["RULE-002"]
