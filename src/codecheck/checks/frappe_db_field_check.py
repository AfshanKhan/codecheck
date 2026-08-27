"""RULE-019: flag a reference to a DocType field that doesn't exist in a
live Frappe site's schema. Only runs when --frappe-db-config is given.

Detects `frappe.db.get_value`/`set_value` and `frappe.get_all`/`get_list`
calls with a literal doctype and literal fieldname(s). A non-plain field
name (contains ".", "(", or " as ") is left alone.

Implemented as a SubRunner, not a HouseCheck, since it needs a live DB
connection."""

from __future__ import annotations

import ast
from pathlib import Path

from codecheck.diff import read_file_content
from codecheck.frappe_db import FrappeDbConnection
from codecheck.models import Finding, ReviewTarget, Severity

_GET_SET_VALUE_METHODS = ("get_value", "set_value")
_GET_ALL_LIST_FUNCTIONS = ("get_all", "get_list")


def _string_value(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_plain_fieldname(name: str) -> bool:
    """Excludes anything that isn't a bare field reference: a child-table-
    qualified name, a SQL function/expression, an alias, or a wildcard."""
    if not name or name == "*":
        return False
    lowered = name.lower()
    return "." not in name and "(" not in name and " as " not in lowered


def _is_frappe_db_attr(func: ast.expr, method: str) -> bool:
    # frappe.db.get_value(...) / frappe.db.set_value(...)
    return (
        isinstance(func, ast.Attribute)
        and func.attr == method
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "db"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "frappe"
    )


def _is_frappe_attr(func: ast.expr, name: str) -> bool:
    # frappe.get_all(...) / frappe.get_list(...)
    return (
        isinstance(func, ast.Attribute)
        and func.attr == name
        and isinstance(func.value, ast.Name)
        and func.value.id == "frappe"
    )


def _extract_field_references(tree: ast.AST) -> list[tuple[str, str, ast.expr]]:
    """Returns (doctype, fieldname, node) triples for every statically-
    resolvable field reference found."""
    results: list[tuple[str, str, ast.expr]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        for method in _GET_SET_VALUE_METHODS:
            if _is_frappe_db_attr(func, method) and len(node.args) >= 3:
                doctype = _string_value(node.args[0])
                if not doctype:
                    break
                field_arg = node.args[2]
                fname = _string_value(field_arg)
                if fname is not None:
                    if _is_plain_fieldname(fname):
                        results.append((doctype, fname, field_arg))
                elif isinstance(field_arg, (ast.List, ast.Tuple)):
                    for elt in field_arg.elts:
                        elt_name = _string_value(elt)
                        if elt_name and _is_plain_fieldname(elt_name):
                            results.append((doctype, elt_name, elt))
                break

        for name in _GET_ALL_LIST_FUNCTIONS:
            if _is_frappe_attr(func, name) and node.args:
                doctype = _string_value(node.args[0])
                if not doctype:
                    break
                for kw in node.keywords:
                    if kw.arg == "fields" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        for elt in kw.value.elts:
                            elt_name = _string_value(elt)
                            if elt_name and _is_plain_fieldname(elt_name):
                                results.append((doctype, elt_name, elt))
                break
    return results


class FrappeDbFieldCheckRunner:
    """Duck-types rules_engine.SubRunner's interface without importing that
    ABC, to avoid a circular import."""

    check_id = "RULE-019"
    title = "Reference to a field that doesn't exist on this DocType"
    severity = Severity.HIGH
    name = "frappe_db_field_check"

    def __init__(self, db: FrappeDbConnection):
        self._db = db

    def is_available(self, repo_path: Path) -> tuple[bool, str | None]:
        return True, None

    def run(self, targets: list[ReviewTarget], repo_path: Path) -> list[Finding]:
        findings = []
        for target in targets:
            if not target.path.endswith(".py") or target.status == "deleted":
                continue
            content = read_file_content(repo_path, target)
            if content is None:
                continue
            try:
                tree = ast.parse(content, filename=target.path)
            except SyntaxError:
                continue

            for doctype, fieldname, node in _extract_field_references(tree):
                if target.changed_lines is not None and node.lineno not in target.changed_lines:
                    continue
                fields = self._db.doctype_fields(doctype)
                if fields is None or fieldname in fields:
                    continue
                findings.append(
                    Finding(
                        check_id=self.check_id,
                        tier="rules",
                        source="house",
                        severity=self.severity,
                        title=self.title,
                        explanation=(
                            f"'{doctype}' has no field named '{fieldname}' in this site's live "
                            "schema (checked against tabDocField and tabCustom Field). This will "
                            "fail or silently return None at runtime, depending on how it's "
                            "accessed -- verify the field name, or that this DocType is the one "
                            "you meant."
                        ),
                        file=target.path,
                        line_start=node.lineno,
                        line_end=node.lineno,
                    )
                )
        return findings
