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

Second fix: diff scope is checked against the same window scanned for
error-handling signals, not just the regex match's own span -- the match
only extends up through the opening "(" (it can't safely balance-match the
closing paren), so a diff that only touches the ".call(" line itself, or
only an argument *inside* the call, would fall outside a match-span-only
range and get skipped even though it's actively touching this call (two
separate Greptile catches, same underlying shape as the RULE-018 fix below).
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
            idx = lineno - 1
            window = lines[idx : idx + _WINDOW]
            window_text = "\n".join(window)
            has_error_handling = bool(_ERROR_SIGNAL_RE.search(window_text))
            has_freeze = bool(_FREEZE_RE.search(window_text))
            if has_error_handling or has_freeze:
                continue
            # The regex match itself only spans up through the opening "(" --
            # it can't safely balance-match the call's closing paren (nested
            # braces, strings containing parens, ...), so a diff that only
            # changes an argument *inside* the call (not the "frappe.call("
            # line itself) would fall outside a range built from the match
            # alone and get skipped even though it's actively touching this
            # call (Greptile catch). Use the same window already scanned for
            # error-handling signals -- consistent with how "near this call"
            # is already defined for that check.
            if changed_lines is not None and changed_lines.isdisjoint(
                range(lineno, lineno + len(window))
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
