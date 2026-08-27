"""RULE-010: flag hardcoded <input>/<button> HTML tags in a Frappe
client-script or an `.html` template. In a `.js` client script, Frappe apps
should build UI through frm/dialog field APIs instead of raw HTML strings, so
it stays consistent with the framework's rendering and events. In an `.html`
Jinja template (a print format, email template, or web page), the same raw
tag means a form input Frappe isn't managing at all -- no validation, no
CSRF handling, nothing tying it back to a DocType field.

The regex is markup-agnostic, so both file types are scanned with the same
pattern (a comparison against a separate audit tool on real repos found real
hardcoded `<input>` tags in `.html` templates this check missed entirely
while only ever looking at `.js`).

Ported from frappe-pr-reviewer's js_analyzer.py (regex/line-based, no JS AST
parser dependency -- matches that tool's approach).
"""

from __future__ import annotations

import re

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_HTML_TAG_RE = re.compile(r"<(input|button)[\s/>]", re.IGNORECASE)


class JsHardcodedHtmlCheck(HouseCheck):
    check_id = "RULE-010"
    title = "Hardcoded <input>/<button> HTML in client script"
    severity = Severity.MEDIUM

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith((".js", ".html")):
            return []

        findings = []
        for lineno, line in enumerate(content.splitlines(), start=1):
            if not _HTML_TAG_RE.search(line):
                continue
            if changed_lines is not None and lineno not in changed_lines:
                continue
            if file_path.endswith(".html"):
                explanation = (
                    "This is a raw <input>/<button> tag in a Jinja template, not something "
                    "generated through a DocType field. Frappe isn't managing it -- no "
                    "validation, no CSRF handling, nothing tying it back to a DocType field."
                )
            else:
                explanation = (
                    "This builds a raw <input>/<button> tag by hand instead of using "
                    "Frappe's field/dialog APIs (frm.add_field, frappe.ui.Dialog, etc.). "
                    "Hand-built HTML skips Frappe's built-in styling, events, and "
                    "accessibility handling."
                )
            findings.append(
                Finding(
                    check_id=self.check_id,
                    tier="rules",
                    source="house",
                    severity=self.severity,
                    title=self.title,
                    explanation=explanation,
                    file=file_path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )
        return findings
