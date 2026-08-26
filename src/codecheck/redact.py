"""Strips locally-identifying details from a ReviewReport before it leaves the
machine that produced it -- `repo_path` is an absolute local filesystem path
(reveals a username, directory layout, sometimes an employer/project name via
the path itself), and that same kind of path can turn up in several other
places: a `skipped` entry echoing back a tool's own error message, a
sub-runner's raw JSON payload (confirmed via Greptile review: ruff's own
`--output-format=json` output carries an absolute `filename` field, which
`RuffRunner` stores unchanged in `Finding.raw` even though the *displayed*
`Finding.file` is already relativized), or -- in principle, since it's
free-form LLM prose -- a finding's title/explanation/suggestion. This is a
narrow, targeted scrub for exactly those cases, not a general PII redactor.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from codecheck.models import Finding, ReviewReport

_PLACEHOLDER = "<local repo>"

# Matches an absolute POSIX or Windows path so it can be replaced wherever one
# shows up inside free-form text (e.g. a linter's own error message embedded
# in a `skipped` entry), not just the dedicated repo_path field. The leading
# `(?<![\w/])` keeps this from matching the internal "/" of an ordinary
# repo-relative path like "app/api.py" -- only a slash that actually starts a
# path segment (string start, or preceded by whitespace/punctuation) counts.
# `(?<!{_PLACEHOLDER})` additionally keeps a second pass from re-matching the
# relative-path remainder immediately following a placeholder this same
# function just inserted (see _scrub_text) -- without it, scrubbing
# "/repo/root/app.py" in two steps (exact repo_path first, then this general
# pattern) would turn the leftover "/app.py" into a second, redundant
# placeholder instead of leaving the actual filename visible.
_ABS_PATH_RE = re.compile(
    rf"(?<![\w/])(?<!{re.escape(_PLACEHOLDER)})(?:/[\w.\-]+)+/?|[A-Za-z]:\\(?:[\w.\-]+\\)*[\w.\-]*"
)


def _scrub_text(text: str, real_path: str) -> str:
    """Replaces `real_path` (the exact repo root) with the placeholder first,
    so whatever comes after it in a path (e.g. "/app.py") survives readable
    -- then scrubs any *other* absolute path found in the text generally.
    """
    scrubbed = text.replace(real_path, _PLACEHOLDER) if real_path else text
    return _ABS_PATH_RE.sub(_PLACEHOLDER, scrubbed)


def _scrub_value(value, real_path: str):
    """Recursively scrubs every string found in an arbitrarily-nested
    JSON-like value (dict/list/str/other) -- needed for Finding.raw, which is
    a sub-runner's own raw output shape (ruff/semgrep JSON) and isn't a fixed,
    known set of fields to scrub by name.
    """
    if isinstance(value, str):
        return _scrub_text(value, real_path)
    if isinstance(value, dict):
        return {k: _scrub_value(v, real_path) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(v, real_path) for v in value]
    return value


def _redact_finding(finding: Finding, real_path: str) -> Finding:
    return dataclasses.replace(
        finding,
        title=_scrub_text(finding.title, real_path),
        explanation=_scrub_text(finding.explanation, real_path),
        suggestion=_scrub_text(finding.suggestion, real_path) if finding.suggestion else None,
        raw=_scrub_value(finding.raw, real_path) if finding.raw else finding.raw,
    )


def redact_report(report: ReviewReport) -> ReviewReport:
    """Returns a new ReviewReport with repo_path replaced by a placeholder,
    every finding's title/explanation/suggestion/raw scrubbed the same way,
    and any absolute-path fragment scrubbed out of the skipped-reason
    strings. Does not mutate the input.
    """
    real_path = report.repo_path
    repo_name = Path(real_path).name if real_path else ""
    display = f"{_PLACEHOLDER} ({repo_name})" if repo_name else _PLACEHOLDER
    return dataclasses.replace(
        report,
        repo_path=display,
        skipped=[_scrub_text(entry, real_path) for entry in report.skipped],
        findings=[_redact_finding(f, real_path) for f in report.findings],
    )
