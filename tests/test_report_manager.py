from datetime import datetime, timezone
from pathlib import Path

from codecheck.models import Finding, ReviewReport, Severity
from codecheck.reporters.manager import ReportManager


def test_report_manager_write_bundle(tmp_path: Path):
    manager = ReportManager(output_dir=tmp_path)
    report = ReviewReport(
        repo_path=str(tmp_path),
        mode="diff",
        base_ref="main",
        head_ref="HEAD",
        generated_at=datetime.now(timezone.utc),
        tiers_run=["rules"],
        findings=[Finding(
            check_id="RULE-001",
            tier="rules",
            source="house",
            severity=Severity.HIGH,
            title="test",
            explanation="test expl",
            file="a.py",
            line_start=1,
        )],
        files_reviewed=["a.py"],
    )

    bundle = manager.write_bundle(report, repo_label="test_repo", pr_number=42)
    assert bundle.json_path is not None and bundle.json_path.exists()
    assert bundle.markdown_path is not None and bundle.markdown_path.exists()
    assert bundle.docx_path is not None and bundle.docx_path.exists()
    assert bundle.xlsx_path is not None and bundle.xlsx_path.exists()
