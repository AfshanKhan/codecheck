"""RULE-024/RULE-025: flag blocking work inside a Frappe document lifecycle
hook (validate, before_save, on_update, ...). RULE-024 covers outbound HTTP
calls; RULE-025 covers synchronous PDF generation. Fix: frappe.enqueue().
Only looks at calls in the hook method's own scope, not nested functions."""

from __future__ import annotations

import ast

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_DOC_EVENT_METHODS = {
    "validate",
    "before_save",
    "before_insert",
    "after_insert",
    "on_update",
    "on_submit",
    "on_cancel",
    "on_trash",
}

_HTTP_CALL_NAMES = {"requests.get", "requests.post", "requests.put", "requests.delete",
                     "requests.patch", "requests.head", "requests.request",
                     "urllib.request.urlopen", "urlopen"}
_PDF_CALL_NAMES = {"pdf.make", "get_pdf", "frappe.get_print", "frappe.get_pdf"}

_NESTED_SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _default_value_exprs(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
    return (d for d in (*node.args.defaults, *node.args.kw_defaults) if d is not None)


def _iter_own_scope(node: ast.AST):
    """Like ast.iter_child_nodes()'s recursive closure, but doesn't descend
    into a nested function/lambda's *body* -- that's deferred until the
    nested callable is actually invoked, so a call inside it shouldn't count
    as running whenever the outer scope runs.

    A nested function/lambda's decorators and default-value expressions are
    the exception: those execute immediately, when the `def`/`lambda`
    statement itself is reached -- not deferred like the body -- so
    `def helper(value=requests.get(url)): ...` inside a doc-event hook
    really does make a blocking call every time that hook runs, even though
    `helper` itself is never called (caught by CodeRabbit review).

    An immediately-invoked lambda (`(lambda: requests.get(url))()`) is a
    second exception -- its body runs right away as part of that call, not
    deferred at all, so it's walked in full rather than treated like an
    ordinary lambda merely defined and passed around for later (also caught
    by CodeRabbit review).
    """
    for child in ast.iter_child_nodes(node):
        yield child
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Lambda):
            lam = child.func
            for default in _default_value_exprs(lam):
                yield default
                yield from _iter_own_scope(default)
            yield lam.body
            yield from _iter_own_scope(lam.body)
            for arg in child.args:
                yield arg
                yield from _iter_own_scope(arg)
            for kw in child.keywords:
                yield kw.value
                yield from _iter_own_scope(kw.value)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in child.decorator_list:
                yield decorator
                yield from _iter_own_scope(decorator)
            for default in _default_value_exprs(child):
                yield default
                yield from _iter_own_scope(default)
        elif isinstance(child, ast.Lambda):
            for default in _default_value_exprs(child):
                yield default
                yield from _iter_own_scope(default)
        else:
            yield from _iter_own_scope(child)


def _dotted_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if not isinstance(func, ast.Attribute):
        return ""
    parts = []
    curr: ast.expr = func
    while isinstance(curr, ast.Attribute):
        parts.insert(0, curr.attr)
        curr = curr.value
    if isinstance(curr, ast.Name):
        parts.insert(0, curr.id)
    return ".".join(parts)


class _DocEventBlockingCallCheck(HouseCheck):
    """Shared scan; subclasses just pick which call-name set they flag."""

    _flagged_names: set[str] = set()

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
        for cls_node in ast.walk(tree):
            if not isinstance(cls_node, ast.ClassDef):
                continue
            for method in cls_node.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if method.name not in _DOC_EVENT_METHODS:
                    continue
                for call in _iter_own_scope(method):
                    if not isinstance(call, ast.Call):
                        continue
                    name = _dotted_name(call.func)
                    if name not in self._flagged_names:
                        continue
                    if changed_lines is not None and call.lineno not in changed_lines:
                        continue
                    findings.append(self._finding(file_path, call, method.name, name))
        return findings

    def _finding(self, file_path: str, call: ast.Call, method_name: str, call_name: str) -> Finding:
        raise NotImplementedError


class BlockingHttpCallInDocEventCheck(_DocEventBlockingCallCheck):
    check_id = "RULE-024"
    title = "Blocking network call in a doc-event hook"
    severity = Severity.MEDIUM
    _flagged_names = _HTTP_CALL_NAMES

    def _finding(self, file_path: str, call: ast.Call, method_name: str, call_name: str) -> Finding:
        return Finding(
            check_id=self.check_id,
            tier="rules",
            source="house",
            severity=self.severity,
            title=self.title,
            explanation=(
                f"'{call_name}' is called inside {method_name}(), a document lifecycle hook "
                "that runs synchronously as part of the save/submit request. If the remote "
                "call is slow or the endpoint is down, the user waits on it directly (or the "
                "save fails outright). Move it to a background job with frappe.enqueue()."
            ),
            file=file_path,
            line_start=call.lineno,
            line_end=call.end_lineno or call.lineno,
        )


class SyncPdfGenerationInDocEventCheck(_DocEventBlockingCallCheck):
    check_id = "RULE-025"
    title = "PDF generation blocks a doc-event hook"
    severity = Severity.LOW
    _flagged_names = _PDF_CALL_NAMES

    def _finding(self, file_path: str, call: ast.Call, method_name: str, call_name: str) -> Finding:
        return Finding(
            check_id=self.check_id,
            tier="rules",
            source="house",
            severity=self.severity,
            title=self.title,
            explanation=(
                f"'{call_name}' is called inside {method_name}(), a document lifecycle hook "
                "that runs synchronously as part of the save/submit request. PDF generation "
                "is slow enough to notice -- move it to a background job with "
                "frappe.enqueue() unless the request genuinely needs the file immediately."
            ),
            file=file_path,
            line_start=call.lineno,
            line_end=call.end_lineno or call.lineno,
        )
