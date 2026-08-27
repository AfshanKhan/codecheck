"""RULE-027: suggest async/await over a frappe.call() Promise chain --
`.then()` callback chains are harder to read and to add error handling to
than an `async function` using `await frappe.call(...)`. A suggestion, not a
correctness issue, so this stays LOW severity.

Whole-file heuristic (mirrors the tool this was ported from): if a file uses
both frappe.call and .then(, and never uses the `async` keyword anywhere,
it's a reasonable bet the file is written in promise-chain style throughout.
Doesn't try to pair a specific .then() with the frappe.call() it follows --
a file-level suggestion this loose isn't precise enough to anchor at one
line with confidence.
"""

from __future__ import annotations

import re

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_THEN_RE = re.compile(r"\.then\s*\(")


class JsAsyncAwaitSuggestionCheck(HouseCheck):
    check_id = "RULE-027"
    title = "frappe.call() Promise chain could use async/await"
    severity = Severity.LOW

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".js"):
            return []
        if "frappe.call" not in content or "async" in content:
            return []
        match = _THEN_RE.search(content)
        if match is None:
            return []
        lineno = content.count("\n", 0, match.start()) + 1
        if changed_lines is not None and lineno not in changed_lines:
            return []
        return [
            Finding(
                check_id=self.check_id,
                tier="rules",
                source="house",
                severity=self.severity,
                title=self.title,
                explanation=(
                    "This file calls frappe.call() and chains .then(), but never uses "
                    "async/await anywhere. An `async function` with `await frappe.call(...)` "
                    "is typically easier to read and to add try/catch error handling to than "
                    "a .then() chain -- worth considering if this file is actively being "
                    "worked on, not necessarily worth a standalone refactor."
                ),
                file=file_path,
                line_start=lineno,
                line_end=lineno,
            )
        ]
