"""RULE-024/RULE-025: flag blocking work done inside a Frappe document
lifecycle hook (validate, before_save, before_insert, after_insert,
on_update, on_submit, on_cancel, on_trash) -- these run synchronously inside
the request that saved/submitted the document, so anything slow in them
makes the user wait on it directly.

RULE-024 covers outbound HTTP calls (requests.*, urllib.request.urlopen).
RULE-025 covers synchronous PDF generation (frappe.get_pdf/get_print,
pdf.make/get_pdf). Both point at the same fix: frappe.enqueue() to move the
work to a background job instead of blocking the save/submit request.

Only looks at calls in the doc-event method's own scope (not a nested
function/lambda's body) -- the same "own scope, not the whole subtree"
distinction RULE-003 (whitelist_permission_check.py) uses, since a blocking
call inside a nested helper that's never actually called from the hook
shouldn't count.
"""

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


def _iter_own_scope(node: ast.AST):
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, _NESTED_SCOPE_TYPES):
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
