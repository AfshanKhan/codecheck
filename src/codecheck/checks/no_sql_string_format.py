"""RULE-002: flag frappe.db.sql() calls built with string formatting (f-strings, %,
.format(), or +) instead of parameterized queries — a SQL injection risk.
"""

from __future__ import annotations

import ast

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity


def _is_frappe_db_sql_call(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "sql"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "db"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "frappe"
    )


def _uses_escape(node: ast.Call) -> bool:
    """True if any name/attribute containing "escape" appears anywhere in the
    call (e.g. frappe.db.escape(value) used to sanitize an interpolated value).
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and "escape" in sub.attr.lower():
            return True
        if isinstance(sub, ast.Name) and "escape" in sub.id.lower():
            return True
    return False


def _query_arg_is_unsafe(query_arg: ast.expr) -> bool:
    if isinstance(query_arg, ast.JoinedStr):  # f-string
        return True
    if isinstance(query_arg, ast.BinOp) and isinstance(query_arg.op, (ast.Mod, ast.Add)):
        return True
    if (
        isinstance(query_arg, ast.Call)
        and isinstance(query_arg.func, ast.Attribute)
        and query_arg.func.attr == "format"
    ):
        return True
    return False


class NoSqlStringFormatCheck(HouseCheck):
    check_id = "RULE-002"
    title = "frappe.db.sql() built with string formatting"
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
            if not (isinstance(node, ast.Call) and _is_frappe_db_sql_call(node)):
                continue
            if not node.args:
                continue
            if changed_lines is not None and node.lineno not in changed_lines:
                continue
            if not _query_arg_is_unsafe(node.args[0]):
                continue
            escaped = _uses_escape(node)
            findings.append(
                Finding(
                    check_id=self.check_id,
                    tier="rules",
                    source="house",
                    severity=Severity.MEDIUM if escaped else self.severity,
                    title=self.title,
                    explanation=(
                        (
                            "frappe.db.sql() query is built with string formatting, but "
                            "frappe.db.escape() is used on the interpolated value(s) -- lower "
                            "risk than an unsanitized value, but still prefer parameterized "
                            "queries: frappe.db.sql(query, params)."
                        )
                        if escaped
                        else (
                            "frappe.db.sql() query is built with string formatting "
                            "(f-string, %, .format(), or +), which risks SQL injection. "
                            "Use parameterized queries: frappe.db.sql(query, params)."
                        )
                    ),
                    file=file_path,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                )
            )
        return findings
