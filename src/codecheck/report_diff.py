"""Compares two prior ReviewReports: which findings are newly introduced
since a baseline run, and which have since been resolved. Powers
`codecheck compare`."""

from __future__ import annotations

from codecheck.models import Finding, ReviewReport


def _finding_key(finding: Finding) -> tuple[str, str, int]:
    """A finding's identity across two runs: file, check, line number."""
    return (finding.file, finding.check_id, finding.line_start)


def diff_reports(old: ReviewReport, new: ReviewReport) -> tuple[list[Finding], list[Finding]]:
    """Returns (added, resolved): findings in `new` but not `old`, and
    findings in `old` but not `new`."""
    old_keys = {_finding_key(f) for f in old.findings}
    new_keys = {_finding_key(f) for f in new.findings}
    added = [f for f in new.findings if _finding_key(f) not in old_keys]
    resolved = [f for f in old.findings if _finding_key(f) not in new_keys]
    return added, resolved
