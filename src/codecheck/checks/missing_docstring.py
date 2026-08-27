"""RULE-033: suggest a docstring on a function/method that doesn't have one.
INFO severity. Skips private (`_leading_underscore`) and dunder names."""

from __future__ import annotations

import ast

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity


def _is_private_or_dunder(name: str) -> bool:
    return name.startswith("_")


class MissingDocstringCheck(HouseCheck):
    check_id = "RULE-033"
    title = "Function is missing a docstring"
    severity = Severity.INFO

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
            if _is_private_or_dunder(node.name):
                continue
            if ast.get_docstring(node):
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
                        f"'{node.name}' has no docstring. A short one-line summary of what it "
                        "does (and any non-obvious parameters) helps future readers, especially "
                        "for a public function other code will call."
                    ),
                    file=file_path,
                    line_start=node.lineno,
                    line_end=node.lineno,
                )
            )
        return findings
