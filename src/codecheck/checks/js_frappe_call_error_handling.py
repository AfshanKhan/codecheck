"""RULE-015: flag frappe.call() invocations with no visible error-handling
signal nearby -- no `error:`/`callback:` handler, no `.catch()`, and no
`freeze: true` to block the UI while the request is in flight. Scans a fixed
window of lines after the call, same heuristic as the source it's ported from.

Ported from frappe-pr-reviewer's js_analyzer.py.
"""

from __future__ import annotations

import re

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_CALL_RE = re.compile(r"frappe\.call\s*\(|(?<![\w.])call\s*\(\s*\{")
_ERROR_SIGNAL_RE = re.compile(r"\b(error|catch|fail|callback)\b", re.IGNORECASE)
_FREEZE_RE = re.compile(r"\bfreeze\s*:")
_WINDOW = 12


class JsFrappeCallErrorHandlingCheck(HouseCheck):
    check_id = "RULE-015"
    title = "frappe.call() with no visible error handling"
    severity = Severity.LOW

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".js"):
            return []

        lines = content.splitlines()
        findings = []
        for idx, line in enumerate(lines):
            if not _CALL_RE.search(line):
                continue
            lineno = idx + 1
            window = lines[idx : idx + _WINDOW]
            window_text = "\n".join(window)
            has_error_handling = bool(_ERROR_SIGNAL_RE.search(window_text))
            has_freeze = bool(_FREEZE_RE.search(window_text))
            if has_error_handling or has_freeze:
                continue
            if changed_lines is not None and lineno not in changed_lines:
                continue
            findings.append(
                Finding(
                    check_id=self.check_id,
                    tier="rules",
                    source="house",
                    severity=self.severity,
                    title=self.title,
                    explanation=(
                        "No error handler (error:/callback:/.catch()) or freeze: true was "
                        "found near this frappe.call(). If the request fails, the failure is "
                        "silent and the UI stays interactive mid-request -- add an error "
                        "handler, or freeze: true if a silent failure is genuinely acceptable "
                        "here."
                    ),
                    file=file_path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )
        return findings
