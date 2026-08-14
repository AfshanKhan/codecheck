"""RULE-008: flag a typed `except` clause whose body is just `pass` (or a
docstring-only stub) -- the error is caught and silently discarded rather than
handled or logged. Distinct from RULE-001 (bare `except:`, which catches
*everything* including KeyboardInterrupt); this rule matches a specific
exception type that's still swallowed silently.
"""

from __future__ import annotations

import ast

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity


def _is_silent_body(body: list[ast.stmt]) -> bool:
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(
        stmt.value.value, str
    ):
        return True
    return False


class SilentExceptionCheck(HouseCheck):
    check_id = "RULE-008"
    title = "Exception caught and silently discarded"
    severity = Severity.MEDIUM

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
            if not (isinstance(node, ast.ExceptHandler) and node.type is not None):
                continue
            if not _is_silent_body(node.body):
                continue
            if changed_lines is not None and node.lineno not in changed_lines:
                continue
            findings.append(
                Finding(
                    check_id=self.check_id,
                    tier="rules",
                    source="house",
                    severity=self.severity,
                    title=self.title,
                    explanation=(
                        "This except clause catches a specific exception but does nothing "
                        "with it, silently discarding the error. If it's genuinely safe to "
                        "ignore, log it (frappe.log_error()) so it's visible when debugging; "
                        "otherwise handle it."
                    ),
                    file=file_path,
                    line_start=node.lineno,
                    line_end=node.lineno,
                )
            )
        return findings
