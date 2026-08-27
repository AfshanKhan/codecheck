"""RULE-020/RULE-021: Frappe app structure checks scoped to hooks.py -- the
one file every Frappe app has that's meant to be pure declarative
configuration, read by the framework itself at boot.

RULE-020 flags hooks.py containing anything beyond simple assignments/
expressions/imports at module level (a function, class, loop, or conditional)
-- logic living there runs on every app boot and is easy to miss during
review, since hooks.py doesn't look like "real" code.

RULE-021 flags a non-empty `override_doctype_class` assignment -- replacing a
standard Frappe DocType's controller class app-wide is a sharp, easy-to-forget
edge that can silently break on a framework upgrade; a doc_events hook is the
narrower, safer way to extend the same lifecycle.
"""

from __future__ import annotations

import ast

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_DECLARATIVE_NODE_TYPES = (ast.Assign, ast.AnnAssign, ast.Expr, ast.Import, ast.ImportFrom)


def _is_hooks_py(file_path: str) -> bool:
    return file_path.endswith("hooks.py")


class HooksPyDeclarativeCheck(HouseCheck):
    check_id = "RULE-020"
    title = "hooks.py contains logic, not just configuration"
    severity = Severity.LOW

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not _is_hooks_py(file_path):
            return []
        try:
            tree = ast.parse(content, filename=file_path)
        except SyntaxError:
            return []

        findings = []
        for node in tree.body:
            if isinstance(node, _DECLARATIVE_NODE_TYPES):
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
                        f"hooks.py contains a {type(node).__name__} at module level -- this "
                        "file is read by Frappe at app boot and is expected to stay purely "
                        "declarative (assignments and imports only). Move any real logic to a "
                        "separate module and import the result instead."
                    ),
                    file=file_path,
                    line_start=node.lineno,
                    line_end=getattr(node, "end_lineno", None) or node.lineno,
                )
            )
        return findings


class DoctypeClassOverrideCheck(HouseCheck):
    check_id = "RULE-021"
    title = "hooks.py overrides a standard DocType controller class"
    severity = Severity.MEDIUM

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not _is_hooks_py(file_path):
            return []
        try:
            tree = ast.parse(content, filename=file_path)
        except SyntaxError:
            return []

        findings = []
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "override_doctype_class"
                for target in node.targets
            ):
                continue
            if isinstance(node.value, ast.Dict) and not node.value.keys:
                continue  # an explicitly empty dict is a no-op, not an override
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
                        "override_doctype_class replaces a standard Frappe DocType's "
                        "controller class app-wide -- a sharp edge that can silently break on "
                        "a framework upgrade, or interact badly with another app also "
                        "overriding the same class. Prefer a doc_events hook to extend the "
                        "same lifecycle method more narrowly, if that covers the need."
                    ),
                    file=file_path,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                )
            )
        return findings
