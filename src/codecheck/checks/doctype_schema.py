"""RULE-022/RULE-023: DocType JSON schema checks. RULE-022 flags a DocType
JSON file that isn't valid JSON (breaks `bench migrate`). RULE-023 flags a
JSON blob stored as a Data/Text field's default value -- it should be a
Table (child table) field instead.
"""

from __future__ import annotations

import json
import re

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_DOCTYPE_JSON_RE = re.compile(r"/doctype/[^/]+/[^/]+\.json$")
_TEXT_FIELDTYPES = {"Data", "Text", "Long Text", "Small Text"}


def _is_doctype_json(file_path: str) -> bool:
    return bool(_DOCTYPE_JSON_RE.search(file_path))


def _line_of(content: str, needle: str) -> int:
    idx = content.find(needle)
    return content.count("\n", 0, idx) + 1 if idx != -1 else 1


_DEFAULT_KEY_RE = re.compile(r'"default"')


def _default_value_line(content: str, fieldname: str) -> int:
    """Line of this field's own "default" key -- not its "fieldname" key.
    A diff that only touches the default *value* itself (the fieldname line
    unchanged) still counts as touching the field the finding is about
    (caught by CodeRabbit review; same shape as the RULE-018/RULE-015 fixes
    elsewhere in this codebase).

    Picks whichever "default" occurrence in the whole file sits *nearest*
    (by character distance) to this field's own "fieldname" occurrence,
    rather than only searching forward from it -- Frappe's real
    `frappe.as_json()` sorts a dict's keys alphabetically, so "default"
    commonly sorts *before* "fieldname" within the same field object, which
    a forward-only search would miss entirely (also caught by CodeRabbit
    review, after the first fix). Not a full JSON-position-aware parser --
    just a best-effort heuristic like the rest of this text-based line
    lookup -- but a real field's own "default" key is virtually always the
    textually closest one to its own "fieldname" key, since adjacent
    fields' keys sit meaningfully farther away in the file.
    """
    fname_idx = content.find(f'"fieldname": "{fieldname}"')
    if fname_idx == -1:
        return _line_of(content, f'"fieldname": "{fieldname}"')
    matches = list(_DEFAULT_KEY_RE.finditer(content))
    if not matches:
        return content.count("\n", 0, fname_idx) + 1
    nearest = min(matches, key=lambda m: abs(m.start() - fname_idx))
    return content.count("\n", 0, nearest.start()) + 1


class DoctypeJsonSyntaxCheck(HouseCheck):
    check_id = "RULE-022"
    title = "DocType JSON file is not valid JSON"
    severity = Severity.MEDIUM

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not _is_doctype_json(file_path):
            return []
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            if changed_lines is not None and e.lineno not in changed_lines:
                return []
            return [
                Finding(
                    check_id=self.check_id,
                    tier="rules",
                    source="house",
                    severity=self.severity,
                    title=self.title,
                    explanation=(
                        f"Failed to parse this DocType definition as JSON: {e.msg} (line "
                        f"{e.lineno}, column {e.colno}). This breaks `bench migrate` for "
                        "every site running this app until it's fixed."
                    ),
                    file=file_path,
                    line_start=e.lineno,
                    line_end=e.lineno,
                )
            ]
        return []


class DoctypeJsonBlobFieldCheck(HouseCheck):
    check_id = "RULE-023"
    title = "JSON blob stored as a text field's default value"
    severity = Severity.LOW

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not _is_doctype_json(file_path):
            return []
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return []  # RULE-022 already reports this; nothing to check here
        if not isinstance(data, dict):
            return []

        findings = []
        for field in data.get("fields", []):
            if not isinstance(field, dict):
                continue
            fieldtype = field.get("fieldtype")
            default = field.get("default")
            if fieldtype not in _TEXT_FIELDTYPES or not isinstance(default, str):
                continue
            stripped = default.strip()
            looks_like_json = (stripped.startswith("{") and stripped.endswith("}")) or (
                stripped.startswith("[") and stripped.endswith("]")
            )
            if not looks_like_json:
                continue
            fieldname = field.get("fieldname", "?")
            lineno = _default_value_line(content, fieldname)
            if changed_lines is not None and lineno not in changed_lines:
                continue
            findings.append(
                Finding(
                    check_id=self.check_id,
                    tier="rules",
                    source="house",
                    severity=self.severity,
                    title=self.title,
                    explanation=(
                        f"Field '{fieldname}' ({fieldtype}) has a JSON-looking string as its "
                        "default value. Storing structured data in a plain text field means "
                        "Frappe can't query, validate, or index its contents -- consider a "
                        "Table (child table) field instead, if the structure is meant to be "
                        "queried or validated."
                    ),
                    file=file_path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )
        return findings
