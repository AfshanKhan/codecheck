"""RULE-003: flag @frappe.whitelist() methods that never call a permission check
(check_permission()/has_permission()) or raise PermissionError anywhere in their
body. Skips allow_guest=True endpoints, which are deliberately public.

Ported from frappe-pr-reviewer's python_analyzer.py, with two fixes: the
original only matched the substring "has_permission", missing the equally-valid
check_permission() pattern (a Document instance method) -- confirmed via a real
false positive on indictranstech/casale_erp#89, where check_permission() was
present but unrecognized. It also used a plain ast.walk() over the whole
function, which both false-negatived (a permission check inside a nested
helper that's never called shouldn't count) and, once that was naively fixed
by not descending into any nested scope, false-positived the opposite way (a
permission check inside a nested helper that IS called should still count).
_has_permission_check() below resolves both: it only descends into a nested
function's body if that function is actually invoked from the outer scope.
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


_NESTED_SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _iter_own_scope(node: ast.AST):
    """Like ast.walk(), but doesn't descend into a nested function/lambda's
    body -- a permission check inside a nested helper that's never called
    shouldn't count as protecting the outer whitelisted endpoint.
    """
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, _NESTED_SCOPE_TYPES):
            yield from _iter_own_scope(child)


def _permission_signal(node: ast.AST) -> bool:
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


def _all_nested_function_defs(func: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every nested function/async-function defined anywhere within func, at
    any depth, keyed by name (lambdas can't be called by name, so they're
    excluded). Flat by design, not scoped to each definition's immediate
    parent: a helper defined as a *sibling* of another nested function (both
    directly inside the outer whitelisted function) is still callable from
    that sibling via a normal Python closure, so scoping the lookup to each
    node's own immediate children would miss that -- confirmed by a real
    regression here (an `_outer` helper calling a sibling `_inner` helper
    that held the actual permission check).
    """
    return {
        node.name: node
        for node in ast.walk(func)
        if node is not func and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _called_names(node: ast.AST) -> set[str]:
    return {
        call.func.id
        for call in _iter_own_scope(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def _has_permission_check(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if func's own body has a permission signal, or if (transitively)
    it calls a nested helper that does. A nested helper that's defined but
    never actually reachable by a call from func doesn't count, since dead
    code can't be protecting anything.
    """
    all_nested = _all_nested_function_defs(func)
    visited: set[int] = set()

    def resolves(node: ast.AST) -> bool:
        if id(node) in visited:
            return False  # guards against mutual/self recursion
        visited.add(id(node))
        if any(_permission_signal(n) for n in _iter_own_scope(node)):
            return True
        return any(
            resolves(all_nested[name]) for name in _called_names(node) if name in all_nested
        )

    return resolves(func)


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
