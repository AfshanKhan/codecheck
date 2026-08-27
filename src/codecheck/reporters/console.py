"""Terminal reporter: grouped by file, colored by severity."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from codecheck.models import SEVERITY_COLOR, ReviewReport, Severity
from codecheck.reporters.glossary import format_ist, source_description, tier_description


def print_report(report: ReviewReport, console: Console) -> None:
    if not report.findings:
        console.print("[green]No findings.[/green]")
        _print_summary(report, console)
    else:
        for file_path, findings in sorted(report.by_file().items()):
            findings = sorted(findings, key=lambda f: (-f.severity.rank, f.line_start))
            table = Table(show_header=True, header_style="bold", expand=True)
            table.add_column("Line", width=6)
            table.add_column("Severity", width=10)
            table.add_column("Check", width=20)
            table.add_column("Title")

            for f in findings:
                color = SEVERITY_COLOR[f.severity]
                # escape() untrusted fields (attacker-controlled file names,
                # linter messages, LLM-generated titles) so they can't inject
                # rich console markup — styling, or clickable [link=...] targets.
                table.add_row(
                    str(f.line_start),
                    f"[{color}]{f.severity.value.upper()}[/{color}]",
                    f"{escape(f.check_id)} ({escape(f.source)})",
                    escape(f.title),
                )
            console.print(Panel(table, title=escape(file_path), title_align="left"))

        _print_summary(report, console)

    if report.skipped:
        console.print("\n[dim]Skipped:[/dim]")
        for line in report.skipped:
            console.print(f"[dim]  - {escape(line)}[/dim]")


def _print_summary(report: ReviewReport, console: Console) -> None:
    counts = report.counts_by_severity()
    parts = []
    for severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO):
        count = counts[severity]
        if count == 0:
            continue
        color = SEVERITY_COLOR[severity]
        parts.append(f"[{color}]{count} {severity.value}[/{color}]")
    summary = ", ".join(parts) if parts else "0 findings"
    console.print(
        f"\n[bold]{len(report.findings)} finding(s)[/bold] across "
        f"{len(report.by_file())} file(s) — {summary}"
    )
    tiers_display = ", ".join(f"{t} ({tier_description(t)})" for t in report.tiers_run)
    console.print(f"[dim]Generated {format_ist(report.generated_at)} — tiers run: {tiers_display}[/dim]")
    sources = sorted({f.source for f in report.findings})
    if sources:
        legend = ", ".join(f"{s} = {source_description(s)}" for s in sources)
        console.print(f"[dim]Check sources: {legend}[/dim]")
