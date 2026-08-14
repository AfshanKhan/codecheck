"""RULE-014: flag raw jQuery DOM manipulation ($(...)/jQuery(...)) in Frappe
client scripts, excluding the framework's own sanctioned entry points
($wrapper, frm.fields_dict) -- Frappe scripts are expected to work through the
frm/dialog APIs rather than reaching into the DOM directly.

Ported from frappe-pr-reviewer's js_analyzer.py.
"""

from __future__ import annotations

import re

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_JQUERY_RE = re.compile(r"(?<![\w$])(?:\$|jQuery)\s*\(")
_SAFE_PATTERNS = ("$wrapper", "frm.fields_dict")


class JsJqueryDomCheck(HouseCheck):
    check_id = "RULE-014"
    title = "Raw jQuery DOM manipulation"
    severity = Severity.LOW

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".js"):
            return []

        findings = []
        for lineno, line in enumerate(content.splitlines(), start=1):
            if not _JQUERY_RE.search(line):
                continue
            if any(safe in line for safe in _SAFE_PATTERNS):
                continue
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
                        "Raw jQuery DOM manipulation reaches outside the frm/dialog APIs "
                        "Frappe scripts are expected to use. Prefer frm.set_df_property(), "
                        "frm.get_field(), or a dialog field instead, if one covers this case."
                    ),
                    file=file_path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )
        return findings
