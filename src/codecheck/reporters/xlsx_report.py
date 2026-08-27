"""Excel (.xlsx) reporter -- the same findings as the other reporters, but in
a shape a reviewer can actually filter and sort in a spreadsheet instead of
scrolling a flat markdown table: a "Findings" sheet with a header-row
AutoFilter (severity/check/file dropdowns, no manual setup needed) plus a
frozen header row, and a "Summary" sheet breaking counts down by severity and
by check ID so it's obvious at a glance which check is generating the most
noise/signal in a given run.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from codecheck.models import ReviewReport, Severity

_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _sanitize(value):
    """Neutralizes a leading formula-trigger character (=, +, -, @, tab, CR)
    on a string value -- a Finding's title/explanation/file/suggestion can
    contain text derived from an untrusted repo's tree or an LLM's read of
    file content, and Excel evaluates a cell starting with one of these as a
    formula when the file is opened (the well-known "CSV/spreadsheet
    injection" class of vulnerability -- a `=HYPERLINK(...)`-style payload
    in a finding title could otherwise run when someone just opens the
    report). Prefixing with an apostrophe is the conventional visual marker
    for "treat as text"; the actual enforcement is each such cell's
    `number_format` being forced to Text in `_write_text_cell` below, so
    Excel never evaluates it as a formula regardless of leading character,
    even for a trigger this specific list doesn't happen to cover.
    """
    if isinstance(value, str) and value[:1] in _FORMULA_TRIGGERS:
        return "'" + value
    return value


def _write_text_cell(ws: Worksheet, row: int, col: int, value) -> None:
    cell = ws.cell(row=row, column=col, value=_sanitize(value))
    cell.number_format = "@"  # Text format -- never evaluated as a formula


_SEVERITY_FILL = {
    Severity.CRITICAL: PatternFill("solid", fgColor="B40000"),
    Severity.HIGH: PatternFill("solid", fgColor="C03B00"),
    Severity.MEDIUM: PatternFill("solid", fgColor="B46400"),
    Severity.LOW: PatternFill("solid", fgColor="1F5CA8"),
    Severity.INFO: PatternFill("solid", fgColor="606060"),
}
_HEADER_FILL = PatternFill("solid", fgColor="2F2F2F")
_HEADER_FONT = Font(color="FFFFFF", bold=True)
_WHITE_BOLD = Font(color="FFFFFF", bold=True)


def _autosize_columns(ws: Worksheet, widths: list[int]) -> None:
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width


def _write_summary_sheet(ws: Worksheet, report: ReviewReport) -> None:
    ws.title = "Summary"
    # repo_path is the one summary value that can reflect an untrusted
    # source (a cloned --repo-url/--pr's own path) -- text-formatted via
    # _write_text_cell like the Findings sheet's free-text columns.
    rows = [
        ("Mode", report.mode),
        ("Base ref", report.base_ref or "-"),
        ("Head ref", report.head_ref or "-"),
        ("Tiers run", ", ".join(report.tiers_run) or "-"),
        ("Generated at", report.generated_at.isoformat()),
        ("Files reviewed", len(report.files_reviewed)),
        ("Duration (s)", round(report.duration_seconds, 1)),
        ("Total findings", len(report.findings)),
    ]
    ws.append(("Repo", None))
    _write_text_cell(ws, 1, 2, report.repo_path)
    for row in rows:
        ws.append(row)
    for cell in ws["A"][: len(rows) + 1]:
        cell.font = Font(bold=True)

    ws.append([])
    header_row = ws.max_row + 1
    ws.append(["Severity", "Count"])
    for cell in ws[header_row]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
    for severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO):
        count = sum(1 for f in report.findings if f.severity == severity)
        if count == 0:
            continue
        ws.append([severity.value.upper(), count])
        cell = ws.cell(row=ws.max_row, column=1)
        cell.fill = _SEVERITY_FILL[severity]
        cell.font = _WHITE_BOLD

    ws.append([])
    header_row = ws.max_row + 1
    ws.append(["Check", "Count", "Highest severity"])
    for cell in ws[header_row]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
    by_check: dict[str, list] = {}
    for f in report.findings:
        by_check.setdefault(f.check_id, []).append(f)
    for check_id in sorted(by_check, key=lambda c: -len(by_check[c])):
        findings = by_check[check_id]
        worst = max(findings, key=lambda f: f.severity.rank).severity
        ws.append([check_id, len(findings), worst.value.upper()])

    if report.skipped:
        ws.append([])
        header_row = ws.max_row + 1
        ws.append(["Skipped"])
        ws.cell(row=header_row, column=1).font = Font(bold=True)
        for entry in report.skipped:
            ws.append([None])
            _write_text_cell(ws, ws.max_row, 1, entry)

    _autosize_columns(ws, [22, 60, 18])


_FINDING_HEADERS = [
    "File", "Line", "Severity", "Check ID", "Source", "Tier",
    "Title", "Explanation", "Suggestion",
]


def _write_findings_sheet(ws: Worksheet, report: ReviewReport) -> None:
    ws.title = "Findings"
    ws.append(_FINDING_HEADERS)
    for cell in ws[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"

    ordered = sorted(report.findings, key=lambda f: (-f.severity.rank, f.file, f.line_start))
    for f in ordered:
        ws.append([
            None,  # File -- written below via _write_text_cell
            f.line_start,
            f.severity.value.upper(),
            f.check_id,
            f.source,
            f.tier,
            None,  # Title -- written below via _write_text_cell
            None,  # Explanation -- written below via _write_text_cell
            None,  # Suggestion -- written below via _write_text_cell
        ])
        row = ws.max_row
        # File/Title/Explanation/Suggestion can all contain text derived from
        # an untrusted repo's own content (a file path, or a finding's own
        # title/explanation/suggestion, some of it LLM-read from file
        # content) -- text-formatted so a leading =/+/-/@ never gets
        # evaluated as a formula when the report is opened.
        _write_text_cell(ws, row, 1, f.file)
        _write_text_cell(ws, row, 7, f.title)
        _write_text_cell(ws, row, 8, f.explanation)
        _write_text_cell(ws, row, 9, f.suggestion or "")
        sev_cell = ws.cell(row=row, column=3)
        sev_cell.fill = _SEVERITY_FILL[f.severity]
        sev_cell.font = _WHITE_BOLD
        for col in (8, 9):  # Explanation / Suggestion -- long free text
            ws.cell(row=row, column=col).alignment = Alignment(wrap_text=True, vertical="top")

    if ws.max_row > 1:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(_FINDING_HEADERS))}{ws.max_row}"
    _autosize_columns(ws, [45, 8, 10, 12, 10, 8, 40, 70, 50])


def render_xlsx(report: ReviewReport) -> openpyxl.Workbook:
    wb = openpyxl.Workbook()
    _write_summary_sheet(wb.active, report)
    findings_ws = wb.create_sheet()
    _write_findings_sheet(findings_ws, report)
    return wb


def write_xlsx_report(report: ReviewReport, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    render_xlsx(report).save(str(output_path))
