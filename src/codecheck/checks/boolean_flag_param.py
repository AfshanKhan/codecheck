"""RULE-034: flag a function parameter with a boolean default value --
`def notify(user, send_email=True)` reads fine at the definition, but a call
site like `notify(user, False)` tells the reader nothing about what `False`
means without going and looking up the signature. Splitting into two
differently-named functions (or using a keyword-only flag, at minimum) makes
call sites self-documenting.
"""

from __future__ import annotations

import ast

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity


class BooleanFlagParamCheck(HouseCheck):
    check_id = "RULE-034"
    title = "Boolean default parameter"
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
            if changed_lines is not None and node.lineno not in changed_lines:
                continue
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if isinstance(default, ast.Constant) and isinstance(default.value, bool):
                    findings.append(
                        Finding(
                            check_id=self.check_id,
                            tier="rules",
                            source="house",
                            severity=self.severity,
                            title=self.title,
                            explanation=(
                                f"'{node.name}' has a boolean default parameter. A call site "
                                "passing the flag positionally (e.g. notify(user, False)) reads "
                                "as meaningless without checking the signature -- consider "
                                "splitting into two functions, or requiring the flag be passed "
                                "by keyword."
                            ),
                            file=file_path,
                            line_start=node.lineno,
                            line_end=node.lineno,
                        )
                    )
                    break  # one finding per function is enough
        return findings
