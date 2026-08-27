import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from codecheck.models import Finding, ReviewReport, Severity
from codecheck.reporters.console import print_report
from codecheck.reporters.docx_report import render_docx, write_docx_report
from codecheck.reporters.json_report import write_json_report
from codecheck.reporters.markdown_report import render_markdown, write_markdown_report
from codecheck.reporters.xlsx_report import render_xlsx, write_xlsx_report


def make_report() -> ReviewReport:
    findings = [
        Finding(
            check_id="RULE-002", tier="rules", source="house", severity=Severity.HIGH,
            title="frappe.db.sql() built with string formatting",
            explanation="SQL injection risk.", file="app/api.py", line_start=42, line_end=42,
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
    )


def test_console_report_runs_without_error():
    console = Console(record=True, width=100)
    print_report(make_report(), console)
    output = console.export_text()
    assert "app/api.py" in output
    assert "RULE-002" in output
    assert "finding(s)" in output


def test_console_report_no_findings():
    console = Console(record=True, width=100)
    empty = ReviewReport(
        repo_path="/repo", mode="diff", base_ref="main", head_ref=None,
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc), tiers_run=["rules"],
    )
    print_report(empty, console)
    assert "No findings" in console.export_text()


def test_console_report_shows_skipped_even_with_no_findings():
    # regression: the "no findings" early-return used to skip the Skipped
    # section entirely, hiding real skip reasons (e.g. a declined LLM tier)
    console = Console(record=True, width=100)
    report = ReviewReport(
        repo_path="/repo", mode="diff", base_ref="main", head_ref=None,
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc), tiers_run=["rules"],
        skipped=["local tier: declined by user"],
    )
    print_report(report, console)
    output = console.export_text()
    assert "No findings" in output
    assert "Skipped" in output
    assert "declined by user" in output


def test_json_report_round_trips(tmp_path: Path):
    output_path = tmp_path / "report.json"
    write_json_report(make_report(), output_path)
    data = json.loads(output_path.read_text())
    assert len(data["findings"]) == 2
    assert data["findings"][0]["check_id"] == "RULE-002"
    assert data["counts_by_severity"]["high"] == 1
    assert data["skipped"] == ["big_file.py: too large for cloud tier"]


def test_markdown_report_contains_table_and_file_headers():
    md = render_markdown(make_report())
    assert "## `app/api.py`" in md
    assert "RULE-002" in md
    assert "| Line | Severity | Check | Title |" in md
    assert "big_file.py" in md


def test_markdown_report_no_findings():
    empty = ReviewReport(
        repo_path="/repo", mode="diff", base_ref="main", head_ref=None,
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc), tiers_run=["rules"],
    )
    md = render_markdown(empty)
    assert "No findings" in md


def test_write_markdown_report(tmp_path: Path):
    output_path = tmp_path / "report.md"
    write_markdown_report(make_report(), output_path)
    assert output_path.exists()
    assert "app/api.py" in output_path.read_text()


def _docx_text(doc) -> str:
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def test_docx_report_contains_findings_and_summary():
    doc = render_docx(make_report())
    text = _docx_text(doc)
    assert "app/api.py" in text
    assert "RULE-002" in text
    assert "big_file.py" in text  # skipped section
    assert "1" in text  # high-severity count


def test_docx_report_no_findings():
    empty = ReviewReport(
        repo_path="/repo", mode="diff", base_ref="main", head_ref=None,
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc), tiers_run=["rules"],
    )
    text = _docx_text(render_docx(empty))
    assert "No findings" in text


def test_write_docx_report(tmp_path: Path):
    output_path = tmp_path / "report.docx"
    write_docx_report(make_report(), output_path)
    assert output_path.exists()
    assert output_path.stat().st_size > 0


def test_xlsx_report_has_summary_and_findings_sheets():
    wb = render_xlsx(make_report())
    assert wb.sheetnames == ["Summary", "Findings"]

    findings_ws = wb["Findings"]
    header = [c.value for c in findings_ws[1]]
    assert header == [
        "File", "Line", "Severity", "Check ID", "Source", "Tier",
        "Title", "Explanation", "Suggestion",
    ]
    # 2 findings + 1 header row
    assert findings_ws.max_row == 3
    rows = [tuple(c.value for c in row) for row in findings_ws.iter_rows(min_row=2)]
    files = [r[0] for r in rows]
    assert "app/api.py" in files
    # highest severity first
    assert rows[0][2] == "HIGH"


def test_xlsx_report_findings_sheet_has_autofilter_and_frozen_header():
    wb = render_xlsx(make_report())
    findings_ws = wb["Findings"]
    assert findings_ws.auto_filter.ref == "A1:I3"
    assert findings_ws.freeze_panes == "A2"


def test_xlsx_report_summary_sheet_has_counts():
    wb = render_xlsx(make_report())
    ws = wb["Summary"]
    values = [cell.value for row in ws.iter_rows() for cell in row if cell.value is not None]
    assert "app/api.py" not in values  # summary doesn't list individual findings
    assert "RULE-002" in values  # per-check breakdown
    assert "HIGH" in values
    assert "big_file.py: too large for cloud tier" in values  # skipped section


def test_xlsx_report_no_findings():
    empty = ReviewReport(
        repo_path="/repo", mode="diff", base_ref="main", head_ref=None,
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc), tiers_run=["rules"],
    )
    wb = render_xlsx(empty)
    findings_ws = wb["Findings"]
    assert findings_ws.max_row == 1  # header only
    assert findings_ws.auto_filter.ref is None  # nothing to filter


def test_write_xlsx_report(tmp_path: Path):
    output_path = tmp_path / "report.xlsx"
    write_xlsx_report(make_report(), output_path)
    assert output_path.exists()
    assert output_path.stat().st_size > 0


def test_xlsx_report_neutralizes_formula_injection_in_untrusted_fields():
    # A Finding's file/title/explanation/suggestion can carry text derived
    # from an untrusted repo's tree or an LLM's read of file content --
    # Excel evaluates a cell starting with =/+/-/@ as a formula when the
    # file is opened (CSV/spreadsheet injection). Every such cell must be
    # both apostrophe-prefixed and Text-formatted, not just one or the
    # other, since only the format is what's actually enforced by Excel.
    f = Finding(
        check_id="RULE-002", tier="rules", source="house", severity=Severity.HIGH,
        title='=HYPERLINK("http://evil.com","click")', explanation="+cmd|/c calc!A1",
        file="-1+1.py", line_start=1, suggestion="@SUM(1,1)",
    )
    report = ReviewReport(
        repo_path="/repo", mode="diff", base_ref="m", head_ref="h",
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        tiers_run=["rules"], findings=[f],
    )
    wb = render_xlsx(report)
    ws = wb["Findings"]
    row = [ws.cell(row=2, column=c) for c in (1, 7, 8, 9)]  # File, Title, Explanation, Suggestion
    for cell in row:
        assert cell.number_format == "@"
        assert not str(cell.value).startswith(("=", "+", "-", "@"))
        assert str(cell.value).startswith("'")


def test_xlsx_report_normal_values_are_untouched():
    wb = render_xlsx(make_report())
    ws = wb["Findings"]
    files = [row[0].value for row in ws.iter_rows(min_row=2)]
    assert "app/api.py" in files


def _injection_report() -> ReviewReport:
    # A finding whose title (LLM-generated from attacker file content) and file
    # name (from an untrusted repo's tree) carry Markdown/console injection.
    f = Finding(
        check_id="CLOUD-001", tier="cloud_llm", source="cloud_llm", severity=Severity.LOW,
        title="ok [x](https://evil) | extra [red]FAKE[/red] [link=https://evil]y[/link]",
        explanation="", file="a`b.py", line_start=1,
    )
    return ReviewReport(
        repo_path="/r", mode="diff", base_ref="m", head_ref="H",
        generated_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        tiers_run=["cloud_llm"], findings=[f],
    )


def test_markdown_report_escapes_untrusted_title_and_path():
    md = render_markdown(_injection_report())
    # the injected Markdown link must not survive as a live link
    assert "[x](https://evil)" not in md
    # the title's '|' must be escaped so it can't add a table column
    assert "\\|" in md
    # a backtick in the file name must not break out of the `code span` header
    assert "a`b.py" not in md


def test_console_report_escapes_untrusted_markup():
    console = Console(record=True, width=200)
    print_report(_injection_report(), console)
    out = console.export_text()
    # injected markup renders as literal text, not interpreted styling/links
    assert "[red]FAKE[/red]" in out
    assert "link=https://evil" in out
