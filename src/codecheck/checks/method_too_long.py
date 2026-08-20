"""RULE-018: flag a function/method body longer than 50 lines -- a length
threshold correlating with reduced readability and testability; long methods
tend to mix multiple responsibilities that are easier to review, test, and
reuse once split apart.

Ported from frappe-pr-reviewer's python_analyzer.py, extended to also cover
`async def` (the original only checked sync `def`), and with one further fix:
diff scope is checked against the function's whole line range, not just its
`def` line -- a diff that only touches the body of an existing >50-line
function (never touching the `def` line itself) still counts as "this
change" touching that function, and should still be flagged (Greptile catch).
"""

from __future__ import annotations

import ast

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_MAX_LINES = 50


class MethodTooLongCheck(HouseCheck):
    check_id = "RULE-018"
    title = "Function is too long"
    severity = Severity.LOW

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".py"):
            return []
        try:
            tree = ast.parse(content, filename=file_path)
        except SyntaxError:
            return []

        findings = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            end_line = node.end_lineno or node.lineno
            length = end_line - node.lineno + 1
            if length <= _MAX_LINES:
                continue
            if changed_lines is not None and changed_lines.isdisjoint(
                range(node.lineno, end_line + 1)
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
                        f"'{node.name}' is {length} lines long (recommended maximum is "
                        f"{_MAX_LINES}). Consider extracting helper functions or splitting "
                        "it into smaller units of responsibility."
                    ),
                    file=file_path,
                    line_start=node.lineno,
                    line_end=node.lineno,
                )
            )
        return findings
