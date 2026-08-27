"""RULE-020/RULE-021: Frappe app structure checks scoped to hooks.py.
RULE-020 flags anything beyond simple assignments/expressions/imports at
module level. RULE-021 flags a non-empty override_doctype_class assignment."""

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
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                # An annotated assignment is just as real an override as a plain one.
                targets, value = [node.target], node.value
            else:
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "override_doctype_class"
                for target in targets
            ):
                continue
            if isinstance(value, ast.Dict) and not value.keys:
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
