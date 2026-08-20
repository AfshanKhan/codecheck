"""RULE-015: flag frappe.call() invocations with no visible error-handling
signal nearby -- no `error:`/`callback:` handler, no `.catch()`, and no
`freeze: true` to block the UI while the request is in flight. Scans a fixed
window of lines after the call, same heuristic as the source it's ported from.

Ported from frappe-pr-reviewer's js_analyzer.py, with one fix: the original
matched "frappe.call(" as a single contiguous substring on one line, missing
the equally-common chained/multi-line call style a formatter (e.g. Prettier)
produces --
    frappe
        .call({
-- where "frappe" and ".call(" land on different lines. Confirmed as a real
gap via a live comparison against frappe-pr-reviewer on a real PR: it found
two frappe.call() findings in a file written exactly this way, codecheck
found zero. _CALL_RE now allows whitespace (including newlines) between
"frappe" and ".call(", and matching runs over the whole file content instead
of line-by-line so a match spanning two lines can actually be found.

Second fix: diff scope is checked against every line the match spans, not
just the line the match starts on -- a diff that only touches the ".call("
line of an existing "frappe\n.call(" pair (the "frappe" line itself unchanged)
still counts as touching this call and should still be flagged (another
Greptile catch, same shape as the RULE-018 fix above it).
"""

from __future__ import annotations

import re

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_CALL_RE = re.compile(r"frappe\s*\.\s*call\s*\(")
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
        for match in _CALL_RE.finditer(content):
            lineno = content.count("\n", 0, match.start()) + 1
            match_end_line = content.count("\n", 0, match.end()) + 1
            idx = lineno - 1
            window = lines[idx : idx + _WINDOW]
            window_text = "\n".join(window)
            has_error_handling = bool(_ERROR_SIGNAL_RE.search(window_text))
            has_freeze = bool(_FREEZE_RE.search(window_text))
            if has_error_handling or has_freeze:
                continue
            if changed_lines is not None and changed_lines.isdisjoint(
                range(lineno, match_end_line + 1)
            ):
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
