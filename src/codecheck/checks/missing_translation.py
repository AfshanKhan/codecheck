"""RULE-006: flag frappe.throw()/frappe.msgprint() calls whose message is a
raw (untranslated) string literal, f-string, or concatenation instead of
being wrapped in _()."""

from __future__ import annotations

import ast

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_FLAGGED_NAMES = {"throw", "msgprint"}


def _is_flagged_call(node: ast.Call) -> bool:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else ""
    )
    return name in _FLAGGED_NAMES


def _is_raw_message(arg: ast.expr) -> bool:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return True
    if isinstance(arg, ast.JoinedStr):  # f-string
        return True
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
        return True
    return False


class MissingTranslationCheck(HouseCheck):
    check_id = "RULE-006"
    title = "User-facing message not wrapped in _()"
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
            if not (isinstance(node, ast.Call) and _is_flagged_call(node)):
                continue
            if not node.args or not _is_raw_message(node.args[0]):
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
                        "This message is a raw string, not wrapped in _(), so it won't be "
                        "translated for non-English users. Wrap it: frappe.throw(_(\"message\"))."
                    ),
                    file=file_path,
                    line_start=node.lineno,
                    line_end=node.lineno,
                )
            )
        return findings
