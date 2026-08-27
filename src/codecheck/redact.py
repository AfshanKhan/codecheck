"""Strips locally-identifying absolute paths from a ReviewReport before it
leaves the machine that produced it -- repo_path, skipped-reason strings, a
sub-runner's raw JSON payload, and finding title/explanation/suggestion. A
narrow, targeted scrub, not a general PII redactor."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from codecheck.models import Finding, ReviewReport

_PLACEHOLDER = "<local repo>"

# A URL-shaped repo_path (--repo-url/--pr) is left as-is, not redacted.
_URL_LIKE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://|^[\w.\-]+@[\w.\-]+:")


def _is_url_like(value: str) -> bool:
    return bool(_URL_LIKE_RE.match(value))

# Matches an absolute POSIX or Windows path anywhere in free-form text.
# `(?<![\w/])` avoids matching inside a relative path like "app/api.py".
# `(?<!{_PLACEHOLDER})` avoids re-matching the remainder right after a
# placeholder _scrub_text already inserted. Interior segments allow any
# character except the separator/newline/colon (not a curated whitelist),
# so punctuation like apostrophes/parens survives being redacted. The final
# segment is narrower (stops at whitespace/colon) so trailing prose after a
# colon-free path isn't swallowed. Colon is excluded everywhere since it's
# the conventional "path: message" separator in linter output.
_ABS_PATH_RE = re.compile(
    rf"(?<![\w/])(?<!{re.escape(_PLACEHOLDER)})"
    r"(?:/[^/:\n]+)*/[^/:\n\s]+/?"
    r"|[A-Za-z]:\\(?:[^\\:\n]+\\)*[^\\:\n\s]*"
)


def _scrub_text(text: str, real_path: str) -> str:
    """Replaces `real_path` with the placeholder first, then scrubs any
    other absolute path found in the text."""
    scrubbed = text.replace(real_path, _PLACEHOLDER) if real_path else text
    return _ABS_PATH_RE.sub(_PLACEHOLDER, scrubbed)


def _scrub_value(value, real_path: str):
    """Recursively scrubs every string in an arbitrarily-nested JSON-like
    value (dict/list/str/other) -- for Finding.raw's unfixed shape."""
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
    """Returns a new ReviewReport with all locally-identifying paths
    scrubbed. Does not mutate the input."""
    real_path = report.repo_path
    if real_path and _is_url_like(real_path):
        display = real_path  # already a public/shareable remote URL
    else:
        repo_name = Path(real_path).name if real_path else ""
        display = f"{_PLACEHOLDER} ({repo_name})" if repo_name else _PLACEHOLDER
    return dataclasses.replace(
        report,
        repo_path=display,
        skipped=[_scrub_text(entry, real_path) for entry in report.skipped],
        findings=[_redact_finding(f, real_path) for f in report.findings],
    )
