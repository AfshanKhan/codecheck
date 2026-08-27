"""RULE-003: flag @frappe.whitelist() methods that never call a permission
check (check_permission()/has_permission()) or raise PermissionError
anywhere in their body. Skips allow_guest=True endpoints.

_has_permission_check() resolves reachability via the real call graph and
name resolution via the real lexical scope chain, so a nested helper only
counts if it's actually called, and a shadowed helper name resolves to the
correct enclosing definition.
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
    """Like ast.walk(), but doesn't descend into a nested function/lambda."""
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


def _build_scope_info(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[dict[int, dict[str, ast.AST]], dict[int, ast.AST]]:
    """Maps each nested scope to its own directly-nested defs by name, and
    to its immediately enclosing scope, for correct lexical name resolution."""
    scope_own_defs: dict[int, dict[str, ast.AST]] = {id(func): {}}
    scope_parent: dict[int, ast.AST] = {}

    def walk(node: ast.AST, scope: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope_own_defs[id(scope)][child.name] = child
                scope_parent[id(child)] = scope
                scope_own_defs[id(child)] = {}
                walk(child, child)
            elif isinstance(child, ast.Lambda):
                pass  # a lambda body is a single expression -- it can't contain a def
            else:
                walk(child, scope)

    walk(func, func)
    return scope_own_defs, scope_parent


def _called_names(node: ast.AST) -> set[str]:
    return {
        call.func.id
        for call in _iter_own_scope(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def _has_permission_check(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if func's own body has a permission signal, or transitively calls
    a nested helper that does. An unreachable helper doesn't count."""
    scope_own_defs, scope_parent = _build_scope_info(func)
    visited: set[int] = set()

    def resolve_name(name: str, scope: ast.AST) -> ast.AST | None:
        current = scope
        while True:
            target = scope_own_defs.get(id(current), {}).get(name)
            if target is not None:
                return target
            if current is func:
                return None
            current = scope_parent[id(current)]

    def resolves(node: ast.AST) -> bool:
        if id(node) in visited:
            return False  # guards against mutual/self recursion
        visited.add(id(node))
        if any(_permission_signal(n) for n in _iter_own_scope(node)):
            return True
        for name in _called_names(node):
            target = resolve_name(name, node)
            if target is not None and resolves(target):
                return True
        return False

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
