"""RULE-022/RULE-023: DocType JSON schema checks -- these run on a DocType's
own definition file (module/doctype/<name>/<name>.json), not on application
code, but the same per-file HouseCheck loop already scans every changed/
audited file regardless of extension, so it's a natural fit.

RULE-022 flags a DocType JSON file that isn't valid JSON at all -- this
breaks `bench migrate` outright, so it's worth catching in review rather
than at deploy time.

RULE-023 flags a JSON blob (a string starting/ending with {}/[]) stored as
the *default* value of an ordinary Data/Text/Long Text/Small Text field --
a sign the field is really being used to hold structured data that belongs
in a Table (child table) field instead, which Frappe can actually query and
validate.

Doesn't attempt the cross-file "does this DocType have a server-side
validate() to match its client-side one" check some tools do here -- a
HouseCheck only ever sees one file's content, with no way to look up a
sibling .py/.js file; RULE-019 already covers spot-checking field references
against a live schema for the cases that need more than one file's context.
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
            lineno = _line_of(content, f'"fieldname": "{fieldname}"')
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
