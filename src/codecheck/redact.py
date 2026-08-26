"""Strips locally-identifying details from a ReviewReport before it leaves the
machine that produced it -- `repo_path` is an absolute local filesystem path
(reveals a username, directory layout, sometimes an employer/project name via
the path itself), and a `skipped` entry can carry the same kind of path if a
tool error message echoed it back. Everything else in a report (file paths,
check IDs, titles, explanations) is already repo-relative or generic, so this
is a narrow, targeted scrub -- not a general PII redactor.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from codecheck.models import ReviewReport

_PLACEHOLDER = "<local repo>"

# Matches an absolute POSIX or Windows path so it can be replaced wherever one
# shows up inside free-form text (e.g. a linter's own error message embedded
# in a `skipped` entry), not just the dedicated repo_path field. The leading
# `(?<![\w/])` keeps this from matching the internal "/" of an ordinary
# repo-relative path like "app/api.py" -- only a slash that actually starts a
# path segment (string start, or preceded by whitespace/punctuation) counts.
_ABS_PATH_RE = re.compile(r"(?<![\w/])(?:/[\w.\-]+)+/?|[A-Za-z]:\\(?:[\w.\-]+\\)*[\w.\-]*")


def _scrub_text(text: str, real_path: str) -> str:
    scrubbed = text.replace(real_path, _PLACEHOLDER) if real_path else text
    return _ABS_PATH_RE.sub(_PLACEHOLDER, scrubbed)


def redact_report(report: ReviewReport) -> ReviewReport:
    """Returns a new ReviewReport with repo_path replaced by a placeholder and
    any absolute-path fragments scrubbed out of the skipped-reason strings.
    Does not mutate the input.
    """
    real_path = report.repo_path
    repo_name = Path(real_path).name if real_path else ""
    display = f"{_PLACEHOLDER} ({repo_name})" if repo_name else _PLACEHOLDER
    return dataclasses.replace(
        report,
        repo_path=display,
        skipped=[_scrub_text(entry, real_path) for entry in report.skipped],
    )
