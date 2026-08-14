"""RULE-010: flag hardcoded <input>/<button> HTML tags in Frappe client-script
JS -- Frappe apps should build UI through frm/dialog field APIs, not raw HTML
strings, so it stays consistent with the framework's rendering and events.

Ported from frappe-pr-reviewer's js_analyzer.py (regex/line-based, no JS AST
parser dependency -- matches that tool's approach).
"""

from __future__ import annotations

import re

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_HTML_TAG_RE = re.compile(r"<(input|button)[\s>]", re.IGNORECASE)


class JsHardcodedHtmlCheck(HouseCheck):
    check_id = "RULE-010"
    title = "Hardcoded <input>/<button> HTML in client script"
    severity = Severity.MEDIUM

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".js"):
            return []

        findings = []
        for lineno, line in enumerate(content.splitlines(), start=1):
            if not _HTML_TAG_RE.search(line):
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
                        "This builds a raw <input>/<button> tag by hand instead of using "
                        "Frappe's field/dialog APIs (frm.add_field, frappe.ui.Dialog, etc.). "
                        "Hand-built HTML skips Frappe's built-in styling, events, and "
                        "accessibility handling."
                    ),
                    file=file_path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )
        return findings
