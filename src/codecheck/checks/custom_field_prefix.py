"""RULE-036: a Custom Field's name must start with `custom_`.

Frappe adds that prefix to every custom field created in the UI, so it can never clash with a standard field
that a later Frappe/ERPNext release adds to the same DocType. A field shipped in an app without it (a fixture,
an exported customization, or `create_custom_fields(...)` in a patch) can silently collide on upgrade.

Looked at: `fixtures/*.json` (Custom Field records), `<module>/custom/<doctype>.json` (exported customizations,
`custom_fields` list) and Python dicts passed to `create_custom_field(s)` or carrying `"doctype": "Custom Field"`.
A field with no `fieldname` is skipped: Frappe derives `custom_<label>` itself. In the two JSON forms a field that
names its own `module` ("GST India", "Income Tax India", ...) is skipped too: exported customizations also carry
fields other installed apps created, and those apps' naming is not the reviewed app's to fix.
"""

from __future__ import annotations

import ast
import json
import re

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

PREFIX = "custom_"
_FIXTURE_JSON_RE = re.compile(r"/fixtures/[^/]+\.json$")
_CUSTOMIZATION_JSON_RE = re.compile(r"/custom/[^/]+\.json$")
_CREATE_FUNCS = {"create_custom_field", "create_custom_fields"}


def _bad(name: object) -> bool:
    return isinstance(name, str) and bool(name) and not name.startswith(PREFIX)


class CustomFieldPrefixCheck(HouseCheck):
    check_id = "RULE-036"
    title = "Custom field name does not start with `custom_`"
    severity = Severity.MEDIUM

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if file_path.endswith(".json"):
            hits = self._json_hits("/" + file_path, content)
        elif file_path.endswith(".py"):
            hits = self._python_hits(content)
        else:
            return []
        return [
            self._finding(file_path, name, dt, line)
            for name, dt, line in hits
            if changed_lines is None or line in changed_lines
        ]

    def _finding(self, file_path: str, name: str, dt: str | None, line: int) -> Finding:
        where = f" on {dt}" if dt else ""
        return Finding(
            check_id=self.check_id,
            tier="rules",
            source="house",
            severity=self.severity,
            title=self.title,
            explanation=(
                f"Custom field '{name}'{where} should be named '{PREFIX}{name}'. Frappe prefixes every "
                f"custom field created in the UI with '{PREFIX}' so it can never clash with a standard "
                "field that a later Frappe or ERPNext version adds to the same DocType; an unprefixed "
                "name can silently collide on upgrade."
            ),
            file=file_path,
            line_start=line,
            line_end=line,
        )

    # --- JSON: fixtures and exported customizations -------------------------------------------------

    def _json_hits(self, path: str, content: str) -> list[tuple[str, str | None, int]]:
        fixture, customization = bool(_FIXTURE_JSON_RE.search(path)), bool(_CUSTOMIZATION_JSON_RE.search(path))
        if not (fixture or customization):
            return []
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return []
        fields: list[dict] = []
        if fixture and isinstance(data, list):
            fields += [d for d in data if isinstance(d, dict) and d.get("doctype") == "Custom Field"]
        if customization and isinstance(data, dict) and isinstance(data.get("custom_fields"), list):
            fields += [d for d in data["custom_fields"] if isinstance(d, dict)]
        seen: dict[str, int] = {}
        hits = []
        for d in fields:
            name = d.get("fieldname")
            if not isinstance(name, str):
                continue
            if d.get("module"):                              # shipped by another app's module, not this app's field
                continue
            n = seen[name] = seen.get(name, 0) + 1          # the n-th field with this name -> its n-th occurrence
            if _bad(name):
                dt = d.get("dt") or (data.get("doctype") if isinstance(data, dict) else None)
                hits.append((name, dt if isinstance(dt, str) else None, _nth_line(content, name, n)))
        return hits

    # --- Python: create_custom_fields(...) and {"doctype": "Custom Field", ...} ---------------------

    def _python_hits(self, content: str) -> list[tuple[str, str | None, int]]:
        if "ustom" not in content:                           # cheap pre-filter: most files never mention it
            return []
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []
        module_values = {
            t.id: node.value
            for node in tree.body if isinstance(node, ast.Assign)
            for t in node.targets if isinstance(t, ast.Name)
        }
        found: dict[int, tuple[str, str | None, int]] = {}

        def add(d: ast.Dict, dt: str | None) -> None:
            name = _const_value(d, "fieldname")
            if name is not None and _bad(name.value):
                found.setdefault(id(d), (name.value, dt or _str_value(d, "dt"), name.lineno))

        for node in ast.walk(tree):
            if isinstance(node, ast.Dict) and _str_value(node, "doctype") == "Custom Field":
                add(node, None)
            elif isinstance(node, ast.Call) and _callee(node) in _CREATE_FUNCS:
                for arg in [*node.args, *(k.value for k in node.keywords)]:
                    if isinstance(arg, ast.Name):            # CUSTOM_FIELDS = {...} defined at module level
                        arg = module_values.get(arg.id, arg)
                    self._walk_fields(arg, None, add)
        return sorted(found.values(), key=lambda h: h[2])

    def _walk_fields(self, node: ast.AST, dt: str | None, add) -> None:
        """Field dicts inside `{"Employee": [{...}, ...]}`, `[{...}]` or a single `{...}`."""
        if isinstance(node, ast.Dict):
            if _const_value(node, "fieldname") is not None:
                add(node, dt)
                return
            for key, value in zip(node.keys, node.values):
                label = key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else dt
                self._walk_fields(value, label, add)
        elif isinstance(node, (ast.List, ast.Tuple)):
            for element in node.elts:
                self._walk_fields(element, dt, add)


def _callee(call: ast.Call) -> str | None:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def _const_value(d: ast.Dict, key: str) -> ast.Constant | None:
    for k, v in zip(d.keys, d.values):
        if isinstance(k, ast.Constant) and k.value == key and isinstance(v, ast.Constant) and isinstance(v.value, str):
            return v
    return None


def _str_value(d: ast.Dict, key: str) -> str | None:
    v = _const_value(d, key)
    return v.value if v is not None else None


def _nth_line(content: str, name: str, n: int) -> int:
    """Line of the n-th `"fieldname": "<name>"` in the file (1 if it cannot be found)."""
    matches = list(re.finditer(r'"fieldname"\s*:\s*"' + re.escape(name) + '"', content))
    if not matches:
        return 1
    m = matches[min(n, len(matches)) - 1]
    return content.count("\n", 0, m.start()) + 1
