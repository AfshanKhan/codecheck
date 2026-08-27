from datetime import datetime, timezone

from codecheck.models import Finding, ReviewReport, Severity


def make_report() -> ReviewReport:
    findings = [
        Finding(
            check_id="RULE-002", tier="rules", source="house", severity=Severity.HIGH,
            title="frappe.db.sql() built with string formatting",
            explanation="SQL injection risk.", file="app/api.py", line_start=42, line_end=42,
            suggestion="Use parameterized queries.",
        ),
        Finding(
            check_id="RUFF-F401", tier="rules", source="ruff", severity=Severity.LOW,
            title="F401: unused import", explanation="Unused import 'os'.",
            file="app/api.py", line_start=1, line_end=1,
        ),
    ]
    return ReviewReport(
        repo_path="/repo", mode="diff", base_ref="main", head_ref="feature",
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        tiers_run=["rules"], findings=findings, files_reviewed=["app/api.py"],
        duration_seconds=1.23, skipped=["big_file.py: too large for cloud tier"],
        file_hashes={"app/api.py": "deadbeef"},
    )


def test_finding_round_trips_through_dict():
    original = Finding(
        check_id="RULE-001", tier="rules", source="house", severity=Severity.MEDIUM,
        title="Bare except clause", explanation="explanation", file="a.py",
        line_start=10, line_end=10, suggestion="Catch a specific exception.",
    )
    restored = Finding.from_dict(original.to_dict())
    assert restored == original


def test_finding_from_dict_missing_required_field_returns_none():
    assert Finding.from_dict({"check_id": "RULE-001"}) is None  # no "file"
    assert Finding.from_dict({"file": "a.py"}) is None  # no "check_id"


def test_finding_from_dict_tolerates_bad_severity():
    finding = Finding.from_dict(
        {"check_id": "RULE-001", "file": "a.py", "severity": "not-a-real-severity"}
    )
    assert finding is not None
    assert finding.severity == Severity.MEDIUM  # Severity.parse's documented fallback


def test_review_report_round_trips_through_dict():
    original = make_report()
    restored = ReviewReport.from_dict(original.to_dict())
    assert restored.repo_path == original.repo_path
    assert restored.mode == original.mode
    assert restored.base_ref == original.base_ref
    assert restored.head_ref == original.head_ref
    assert restored.generated_at == original.generated_at
    assert restored.tiers_run == original.tiers_run
    assert restored.files_reviewed == original.files_reviewed
    assert restored.duration_seconds == original.duration_seconds
    assert restored.skipped == original.skipped
    assert restored.file_hashes == original.file_hashes
    assert [f.check_id for f in restored.findings] == [f.check_id for f in original.findings]
    assert restored.findings[0].suggestion == "Use parameterized queries."


def test_review_report_from_dict_tolerates_missing_fields():
    report = ReviewReport.from_dict({})
    assert report.repo_path == ""
    assert report.mode == "diff"
    assert report.findings == []
    assert report.tiers_run == []


def test_review_report_from_dict_tolerates_non_string_generated_at():
    # regression (Greptile): datetime.fromisoformat() raises TypeError (not
    # ValueError) for a non-string argument -- a truthy, non-string,
    # malformed generated_at (an int, a list, ...) used to escape the
    # documented "tolerant of a malformed generated_at" fallback and crash
    # render/compare instead of falling back to now().
    for bad_value in (12345, ["not", "a", "string"], {"nested": "dict"}, True):
        report = ReviewReport.from_dict({"generated_at": bad_value})
        assert isinstance(report.generated_at, datetime)  # fell back, didn't crash


def test_review_report_from_dict_drops_malformed_findings_not_the_whole_report():
    data = make_report().to_dict()
    data["findings"].append({"check_id": "RULE-999"})  # missing "file" -- malformed
    data["findings"].append("not even a dict")
    report = ReviewReport.from_dict(data)
    assert len(report.findings) == 2  # the two well-formed ones survive


def test_compliance_percentage_is_100_for_a_clean_run():
    report = ReviewReport(
        repo_path="/r", mode="diff", base_ref="m", head_ref="h",
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc), tiers_run=["rules"],
        files_reviewed=["a.py", "b.py"],
    )
    assert report.compliance_percentage() == 100.0


def test_compliance_percentage_is_100_when_nothing_was_reviewed():
    # no files_reviewed -- avoid a division by zero, and there's nothing to
    # be non-compliant about, so treat it the same as a clean run rather
    # than 0%.
    report = ReviewReport(
        repo_path="/r", mode="diff", base_ref="m", head_ref="h",
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc), tiers_run=["rules"],
    )
    assert report.compliance_percentage() == 100.0


def test_compliance_percentage_weights_by_severity():
    def report_with(severity: Severity) -> ReviewReport:
        finding = Finding(
            check_id="RULE-001", tier="rules", source="house", severity=severity,
            title="t", explanation="e", file="a.py", line_start=1,
        )
        return ReviewReport(
            repo_path="/r", mode="diff", base_ref="m", head_ref="h",
            generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc), tiers_run=["rules"],
            findings=[finding], files_reviewed=["a.py", "b.py"],
        )

    # 1 CRITICAL on 1 of 2 files costs that file its entire (capped) credit --
    # 50% overall, same as a full FAIL on half the files reviewed.
    assert report_with(Severity.CRITICAL).compliance_percentage() == 50.0
    # A LOW finding costs proportionally less.
    assert report_with(Severity.LOW).compliance_percentage() == 87.5


def test_compliance_percentage_caps_per_file_penalty_at_full_credit():
    # regression: multiple findings piling up on one file must not drag that
    # file's credit below zero (which would need an even worse-scoring
    # second file to "cancel out"), the same way FAIL never scores worse
    # than FAIL in the audit-tool scheme this was adapted from.
    findings = [
        Finding(
            check_id="RULE-001", tier="rules", source="house", severity=Severity.CRITICAL,
            title="t", explanation="e", file="a.py", line_start=i,
        )
        for i in range(1, 6)  # 5 CRITICAL findings, way past a single file's max weight
    ]
    report = ReviewReport(
        repo_path="/r", mode="diff", base_ref="m", head_ref="h",
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc), tiers_run=["rules"],
        findings=findings, files_reviewed=["a.py", "b.py"],
    )
    # a.py is capped at 0 credit (not negative), b.py is untouched (full
    # credit) -- 50%, not less.
    assert report.compliance_percentage() == 50.0


def test_compliance_percentage_included_in_to_dict():
    report = make_report()
    assert report.to_dict()["compliance_percentage"] == report.compliance_percentage()
