"""RULE-026: flag a `.save()` call inside a loop -- saving documents one at a
time in a loop means one DB round-trip (plus all of Document.save()'s
validation/hook overhead) per iteration. For a large dataset, Frappe's own
bulk helpers (frappe.db.bulk_update(), or building a bulk insert) are
dramatically faster since they collapse that into far fewer round-trips.

Distinct from RULE-004 (n_plus_one_query.py), which flags *read* calls
(get_value/get_all/...) in a loop -- this is the write-side equivalent.

Doesn't descend into a nested function/lambda *body* defined inside a loop --
a callback registered for later (`frappe.enqueue(deferred)`, an event
handler, ...) doesn't actually run once per loop iteration just because it
was *defined* once per iteration; only a call that's actually reached while
the loop body itself executes counts (caught by CodeRabbit review). Its
decorators and default-value expressions are the exception, since those
genuinely do run immediately as part of defining it, once per iteration --
same distinction blocking_call_in_doc_event.py's `_iter_own_scope` makes.
"""

from __future__ import annotations

import ast

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_LOOP_NODE_TYPES = (ast.For, ast.AsyncFor, ast.While)
_NESTED_SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _is_save_call(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "save"


def _default_value_exprs(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
    return (d for d in (*node.args.defaults, *node.args.kw_defaults) if d is not None)


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
            # Only special-case a nested function/lambda's body once we're
            # actually inside a loop (next_depth > 0) -- outside of any
            # loop, descending into a function's body normally is exactly
            # how a loop *inside* that function (e.g. validate()'s own
            # body) gets found in the first place. Regression caught live:
            # an earlier version of this fix applied the same skip
            # unconditionally, which stopped finding a direct `d.save()`
            # inside an ordinary `for` loop in a top-level method at all.
            if next_depth > 0 and isinstance(child, _NESTED_SCOPE_TYPES):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for decorator in child.decorator_list:
                        self._walk(decorator, next_depth, file_path, changed_lines, out)
                for default in _default_value_exprs(child):
                    self._walk(default, next_depth, file_path, changed_lines, out)
                # child.body is deliberately not walked here -- deferred.
            else:
                self._walk(child, next_depth, file_path, changed_lines, out)
