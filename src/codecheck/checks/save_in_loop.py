"""RULE-026: flag a `.save()` call inside a loop -- saving documents one at a
time in a loop means one DB round-trip (plus all of Document.save()'s
validation/hook overhead) per iteration. For a large dataset, Frappe's own
bulk helpers (frappe.db.bulk_update(), or building a bulk insert) are
dramatically faster since they collapse that into far fewer round-trips.

Distinct from RULE-004 (n_plus_one_query.py), which flags *read* calls
(get_value/get_all/...) in a loop -- this is the write-side equivalent.
"""

from __future__ import annotations

import ast

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_LOOP_NODE_TYPES = (ast.For, ast.AsyncFor, ast.While)


def _is_save_call(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "save"


class SaveInLoopCheck(HouseCheck):
    check_id = "RULE-026"
    title = ".save() call inside a loop"
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

        findings: list[Finding] = []
        self._walk(tree, loop_depth=0, file_path=file_path, changed_lines=changed_lines, out=findings)
        return findings

    def _walk(
        self,
        node: ast.AST,
        loop_depth: int,
        file_path: str,
        changed_lines: set[int] | None,
        out: list[Finding],
    ) -> None:
        is_loop = isinstance(node, _LOOP_NODE_TYPES)
        next_depth = loop_depth + 1 if is_loop else loop_depth

        if isinstance(node, ast.Call) and loop_depth > 0 and _is_save_call(node):
            if changed_lines is None or node.lineno in changed_lines:
                out.append(
                    Finding(
                        check_id=self.check_id,
                        tier="rules",
                        source="house",
                        severity=self.severity,
                        title=self.title,
                        explanation=(
                            "This .save() runs once per loop iteration, each one paying the "
                            "full cost of a DB round-trip plus Document.save()'s own validation "
                            "and hooks. For a large dataset, consider "
                            "frappe.db.bulk_update()/bulk_insert() instead."
                        ),
                        file=file_path,
                        line_start=node.lineno,
                        line_end=node.end_lineno or node.lineno,
                    )
                )

        for child in ast.iter_child_nodes(node):
            self._walk(child, next_depth, file_path, changed_lines, out)
