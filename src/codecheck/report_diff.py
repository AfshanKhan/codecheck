"""Compares two prior ReviewReports to answer "what changed" -- which findings
are newly introduced since a baseline run, and which have since been resolved.
Powers `codecheck compare`, e.g. an audit pinned as a baseline against a later
audit of the same repo, to track whether a codebase is trending better or worse
over time instead of only ever seeing one point-in-time snapshot.
"""

from __future__ import annotations

from codecheck.models import Finding, ReviewReport


def _finding_key(finding: Finding) -> tuple[str, str, int]:
    """A finding's identity across two runs: which file, which check, roughly
    which line. Line number alone (not a range or exact content match) is
    deliberate -- a finding surviving a few lines of unrelated code shifting
    around it should still count as "the same finding," not a resolved one
    plus a coincidentally-new one.
    """
    return (finding.file, finding.check_id, finding.line_start)


def diff_reports(old: ReviewReport, new: ReviewReport) -> tuple[list[Finding], list[Finding]]:
    """Returns (added, resolved): findings present in `new` but not `old`
    (newly introduced since the baseline), and findings present in `old` but
    not `new` (no longer present -- fixed, or the code around them changed
    enough that the finding's identity no longer matches).
    """
    old_keys = {_finding_key(f) for f in old.findings}
    new_keys = {_finding_key(f) for f in new.findings}
    added = [f for f in new.findings if _finding_key(f) not in old_keys]
    resolved = [f for f in old.findings if _finding_key(f) not in new_keys]
    return added, resolved
