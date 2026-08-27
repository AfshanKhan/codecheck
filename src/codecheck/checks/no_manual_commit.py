"""RULE-005: flag frappe.db.commit()/db.commit() calls. Frappe auto-commits
at the end of a successful request; a manual commit mid-request can commit
a partial transaction if something later fails."""

from __future__ import annotations

import ast

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity


def _is_db_commit_call(node: ast.Call) -> bool:
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "commit"):
        return False
    value = func.value
    if isinstance(value, ast.Name):
        return value.id == "db"
    if isinstance(value, ast.Attribute):
        return value.attr == "db"
    return False


class NoManualCommitCheck(HouseCheck):
    check_id = "RULE-005"
    title = "Manual frappe.db.commit() call"
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
            if not (isinstance(node, ast.Call) and _is_db_commit_call(node)):
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
                        "Frappe auto-commits at the end of a successful request; a manual "
                        "db.commit() mid-request can commit a partial transaction if later "
                        "code in the same request raises. Remove it unless there's a specific, "
                        "documented reason a partial commit here is safe (e.g. a long-running "
                        "background job that intentionally checkpoints)."
                    ),
                    file=file_path,
                    line_start=node.lineno,
                    line_end=node.lineno,
                )
            )
        return findings
