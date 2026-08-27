"""RULE-004: flag Frappe DB/document-fetch calls made inside a loop body --
the classic N+1 query pattern."""

from __future__ import annotations

import ast

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_FLAGGED_ATTRS = {
    "get_doc",
    "get_all",
    "get_list",
    "get_meta",
    "get_value",
    "get_values",
    "get_single_value",
    "set_value",
    "sql",
    "exists",
    "count",
}

_LOOP_NODE_TYPES = (
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ListComp,
    ast.DictComp,
    ast.SetComp,
    ast.GeneratorExp,
)


def _is_flagged_call(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _FLAGGED_ATTRS:
        return False
    # Only frappe.get_all/db.get_value/self.db_... style calls, not a bare function.
    value = func.value
    if isinstance(value, ast.Name):
        return value.id in ("frappe", "db", "self")
    if isinstance(value, ast.Attribute):
        return value.attr in ("db",) or (
            isinstance(value.value, ast.Name) and value.value.id == "frappe"
        )
    return False


class NPlusOneQueryCheck(HouseCheck):
    check_id = "RULE-004"
    title = "Database call inside a loop (possible N+1 query)"
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

        if isinstance(node, ast.Call) and loop_depth > 0 and _is_flagged_call(node):
            if changed_lines is None or node.lineno in changed_lines:
                out.append(
                    Finding(
                        check_id=self.check_id,
                        tier="rules",
                        source="house",
                        severity=self.severity,
                        title=self.title,
                        explanation=(
                            "This database call runs once per loop iteration instead of "
                            "being batched before the loop. For large datasets this turns "
                            "into many round-trips (N+1 queries) -- consider fetching once "
                            "with a single get_all()/get_list() call and looking up results "
                            "from an in-memory dict inside the loop."
                        ),
                        file=file_path,
                        line_start=node.lineno,
                        line_end=node.end_lineno or node.lineno,
                    )
                )

        for child in ast.iter_child_nodes(node):
            self._walk(child, next_depth, file_path, changed_lines, out)
