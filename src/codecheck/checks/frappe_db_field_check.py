"""RULE-019: flag a reference to a DocType field that doesn't actually exist
in a live Frappe site's schema -- something no purely static analysis can
ever know (a field could be renamed, removed, or simply never existed, and
the code would still look syntactically fine). Only runs when
`--frappe-db-config` is given; see docs/Configuration.md.

Detects the statically-resolvable cases: `frappe.db.get_value`/`set_value`
with a literal doctype and a literal fieldname (or list of fieldnames), and
`frappe.get_all`/`get_list` with a literal doctype and a literal `fields=[...]`
list. A field name that isn't a plain identifier (contains ".", "(", or " as "
-- a child-table-qualified reference, a SQL function call, or an alias) is
left alone; those aren't verifiable this way, and guessing would risk a false
positive on a genuinely valid, just-not-plain-field expression.

Not a HouseCheck: it needs a live database connection to judge anything,
which the HouseCheck interface (file content only) has no way to provide.
Implemented as its own SubRunner (FrappeDbFieldCheckRunner below), wired in by
cli.py only when a working --frappe-db-config connection was established.
"""

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
    qualified name ("customer.customer_name"), a SQL function/expression
    ("count(name)"), an alias ("name as party"), or a wildcard ("*") -- none
    of those are a single field's existence question, and guessing at one
    from the fragment risks a false positive.
    """
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
    resolvable field reference found -- `node` is used for the finding's line
    number, pointing at the fieldname literal itself rather than the whole call.
    """
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
    """Duck-types rules_engine.SubRunner's interface (is_available/run/name)
    without importing that ABC, deliberately -- rules_engine.py imports this
    module to wire the runner in, so importing SubRunner back from there
    would create a circular import. Nothing in RulesEngineReviewer.review()
    actually isinstance-checks against SubRunner, only calls these methods.
    """

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
