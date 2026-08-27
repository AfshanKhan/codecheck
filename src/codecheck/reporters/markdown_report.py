"""Markdown reporter, for pasting into a PR description or Slack."""

from __future__ import annotations

from pathlib import Path

from codecheck.models import Finding, ReviewReport, Severity
from codecheck.reporters.glossary import format_ist, source_description, tier_description

_SEVERITY_EMOJI = {
    Severity.CRITICAL: "\U0001f6d1",  # stop sign
    Severity.HIGH: "\U0001f534",  # red circle
    Severity.MEDIUM: "\U0001f7e1",  # yellow circle
    Severity.LOW: "\U0001f535",  # blue circle
    Severity.INFO: "⚪",  # white circle
}


def _md_inline(text: str) -> str:
    """Neutralize Markdown injection from untrusted text (LLM-generated titles,
    attacker-controlled file names) used in a table cell. These reports are meant
    to be pasted into a PR description or Slack, so links/images/raw HTML and
    table-structure breakage (`|`, newlines) must not survive."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for ch in "\\`*_[]()~|":
        text = text.replace(ch, "\\" + ch)
    return text.replace("\r", " ").replace("\n", " ")


def _md_code(text: str) -> str:
    """Sanitize untrusted text for use inside a `code span`: a code span can't be
    backslash-escaped, so a stray backtick would break out of it (and a newline
    out of the line entirely)."""
    return text.replace("`", "'").replace("\r", " ").replace("\n", " ")


def render_markdown(report: ReviewReport) -> str:
    lines = ["# PR Review Report", ""]

    lines.append(f"**Repo:** {_md_inline(report.repo_path)}  ")
    lines.append(f"**Generated:** {format_ist(report.generated_at)}")
    lines.append("")

    counts = report.counts_by_severity()
    summary_bits = [
        f"{_SEVERITY_EMOJI[s]} {counts[s]} {s.value}"
        for s in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO)
        if counts[s]
    ]
    lines.append(f"**{len(report.findings)} finding(s)** — " + ", ".join(summary_bits) if summary_bits else "**No findings.**")
    lines.append("")

    tier_bits = [f"`{t}` ({tier_description(t)})" for t in report.tiers_run]
    lines.append(f"_Tiers run: {', '.join(tier_bits)}_")
    lines.append("")

    sources = sorted({f.source for f in report.findings})
    if sources:
        source_bits = ", ".join(f"**{s}** = {source_description(s)}" for s in sources)
        lines.append(
            f"<sub>Each finding below is tagged `CHECK-ID` (source) — {source_bits}.</sub>"
        )
        lines.append("")

    for file_path, findings in sorted(report.by_file().items()):
        lines.append(f"## `{_md_code(file_path)}`")
        lines.append("")
        lines.append("| Line | Severity | Check | Title |")
        lines.append("|---|---|---|---|")
        for f in sorted(findings, key=lambda x: (-x.severity.rank, x.line_start)):
            lines.append(_row(f))
        lines.append("")

    if report.skipped:
        lines.append("## Skipped")
        lines.append("")
        for entry in report.skipped:
            # entry is a codecheck-composed string, but embeds untrusted paths;
            # collapse newlines so a crafted path can't inject extra list items.
            lines.append(f"- {entry.replace(chr(13), ' ').replace(chr(10), ' ')}")
        lines.append("")

    return "\n".join(lines)


def _row(f: Finding) -> str:
    emoji = _SEVERITY_EMOJI[f.severity]
    return (
        f"| {f.line_start} | {emoji} {f.severity.value} "
        f"| `{_md_code(f.check_id)}` ({_md_code(f.source)}) | {_md_inline(f.title)} |"
    )


def write_markdown_report(report: ReviewReport, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_markdown(report))
