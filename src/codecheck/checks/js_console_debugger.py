"""RULE-012/RULE-013: flag leftover console.log() and debugger; statements in
client-script JS. console.log is debug noise; debugger; is worse -- it halts
execution for anyone with devtools open, including end users.

Ported from frappe-pr-reviewer's js_analyzer.py.
"""

from __future__ import annotations

import re

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_CONSOLE_LOG_RE = re.compile(r"console\.(log|debug)\s*\(")
_DEBUGGER_RE = re.compile(r"(?<![\w.])debugger\s*;")


class JsConsoleLogCheck(HouseCheck):
    check_id = "RULE-012"
    title = "Leftover console.log()"
    severity = Severity.LOW

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".js"):
            return []

        findings = []
        for lineno, line in enumerate(content.splitlines(), start=1):
            if not _CONSOLE_LOG_RE.search(line):
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
                    explanation="Leftover console.log()/console.debug() -- almost always debug output. Remove it.",
                    file=file_path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )
        return findings


class JsDebuggerStatementCheck(HouseCheck):
    check_id = "RULE-013"
    title = "Leftover debugger; statement"
    severity = Severity.HIGH

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".js"):
            return []

        findings = []
        for lineno, line in enumerate(content.splitlines(), start=1):
            if not _DEBUGGER_RE.search(line):
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
                        "A `debugger;` statement halts execution for any user with devtools "
                        "open. This should never reach production -- remove it."
                    ),
                    file=file_path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )
        return findings
