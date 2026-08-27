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

# `report.repo_path` is a remote URL, not a local filesystem path, whenever
# the review came from --repo-url or a --pr URL -- nothing local-identifying
# about a public (or already-authenticated-to) remote URL, so it's left
# exactly as-is rather than being replaced with the placeholder like a real
# local path is.
_URL_LIKE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://|^[\w.\-]+@[\w.\-]+:")


def _is_url_like(value: str) -> bool:
    return bool(_URL_LIKE_RE.match(value))

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
#
# Interior path segments (ones followed by another separator, so they're
# bounded on both sides) match any run of characters other than the segment
# separator, a newline, or a colon (not a curated `[\w.\- ]` class) -- a real
# path component can contain apostrophes, parentheses, plus signs, or
# anything else a filesystem allows ("/Users/O'Brien's Files/project
# (backup)/app.py"), and a whitelist-style character class only redacts the
# prefix up to the first character outside it, leaving the identifying
# suffix (often the actual username) exposed in the written report
# (confirmed real via Greptile security review, twice -- first for spaces,
# then again for other punctuation).
#
# The FINAL segment (the filename or directory the path actually ends on --
# unbounded on the right, since nothing marks where the path text stops and
# unrelated prose begins) is deliberately narrower: it stops at the first
# whitespace or colon, rather than being just as permissive as an interior
# segment. Real path components almost always end in a bare filename with no
# embedded space (app.py, config.json); it's specifically *this* final,
# open-ended segment that risked swallowing an entire trailing diagnostic
# message word-by-word when made fully permissive (confirmed real via
# Greptile review: "at /repo/app.py exceeds size limit, see docs for
# details" lost everything after "app.py"). This narrows that one specific
# risk back down -- the rare case of a path's *final* component itself
# containing a space (a bare directory, not a file, as the very last thing
# on the line) is a strictly smaller, already-improved-on regression versus
# matching nothing at all. Colon stays excluded from every segment because
# it's the conventional "path: message" separator in linter/tool output
# (eslint, semgrep, ...). Newlines are excluded throughout so this can't
# swallow past the end of the current line in a multi-line message.
_ABS_PATH_RE = re.compile(
    rf"(?<![\w/])(?<!{re.escape(_PLACEHOLDER)})"
    r"(?:/[^/:\n]+)*/[^/:\n\s]+/?"
    r"|[A-Za-z]:\\(?:[^\\:\n]+\\)*[^\\:\n\s]*"
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
