"""RULE-003: flag @frappe.whitelist() methods that never call a permission check
(check_permission()/has_permission()) or raise PermissionError anywhere in their
body. Skips allow_guest=True endpoints, which are deliberately public.

Ported from frappe-pr-reviewer's python_analyzer.py, with one fix: the original
only matched the substring "has_permission", missing the equally-valid
check_permission() pattern (a Document instance method) -- confirmed via a real
false positive on indictranstech/casale_erp#89, where check_permission() was
present but unrecognized.
"""

from __future__ import annotations

import ast

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity


def _is_whitelist_decorator(dec: ast.expr) -> bool:
    if isinstance(dec, ast.Call):
        dec = dec.func
    if isinstance(dec, ast.Attribute):
        return dec.attr == "whitelist"
    if isinstance(dec, ast.Name):
        return dec.id == "whitelist"
    return False


def _allows_guest(dec: ast.expr) -> bool:
    if not isinstance(dec, ast.Call):
        return False
    for kw in dec.keywords:
        if kw.arg == "allow_guest" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    return False


def _has_permission_check(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            name = target.attr if isinstance(target, ast.Attribute) else (
                target.id if isinstance(target, ast.Name) else ""
            )
            if "has_permission" in name or "check_permission" in name:
                return True
        if isinstance(node, ast.Raise) and node.exc is not None:
            exc = node.exc
            if isinstance(exc, ast.Call):
                exc = exc.func
            exc_name = exc.attr if isinstance(exc, ast.Attribute) else (
                exc.id if isinstance(exc, ast.Name) else ""
            )
            if "Permission" in exc_name:
                return True
    return False


class WhitelistPermissionCheck(HouseCheck):
    check_id = "RULE-003"
    title = "@frappe.whitelist() method has no permission check"
    severity = Severity.HIGH

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
            whitelist_decs = [d for d in node.decorator_list if _is_whitelist_decorator(d)]
            if not whitelist_decs:
                continue
            if any(_allows_guest(d) for d in whitelist_decs):
                continue
            if _has_permission_check(node):
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
                        f"'{node.name}' is exposed via @frappe.whitelist() but its body "
                        "never calls a permission check (check_permission()/has_permission()) "
                        "or raises PermissionError. Any logged-in user can call this endpoint -- "
                        "verify access is actually restricted, or mark it allow_guest=True if "
                        "it's deliberately public."
                    ),
                    file=file_path,
                    line_start=node.lineno,
                    line_end=node.lineno,
                )
            )
        return findings
