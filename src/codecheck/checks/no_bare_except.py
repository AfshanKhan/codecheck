"""RULE-001: flag bare `except:` clauses, which swallow all exceptions including
KeyboardInterrupt/SystemExit and hide real bugs.
"""

from __future__ import annotations

import ast

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity


class NoBareExceptCheck(HouseCheck):
    check_id = "RULE-001"
    title = "Bare except clause"
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
            if isinstance(node, ast.ExceptHandler) and node.type is None:
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
                            "Bare `except:` catches all exceptions, including "
                            "KeyboardInterrupt and SystemExit, and can hide real bugs. "
                            "Catch a specific exception type instead."
                        ),
                        file=file_path,
                        line_start=node.lineno,
                        line_end=node.lineno,
                    )
                )
        return findings
