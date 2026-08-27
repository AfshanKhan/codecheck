"""RULE-014: flag raw jQuery DOM manipulation ($(...)/jQuery(...)) in Frappe
client scripts, excluding the framework's own sanctioned entry points
($wrapper, frm.fields_dict) -- Frappe scripts are expected to work through the
frm/dialog APIs rather than reaching into the DOM directly.

The "sanctioned entry point" exemption only applies when it's the argument
being wrapped, e.g. `$($wrapper)` or `$(frm.fields_dict.my_field.$wrapper)`
-- an earlier version exempted any line containing the substring "$wrapper"
*anywhere*, which also matched a genuinely raw jQuery call wrapping a
different object that merely has a same-named property, e.g.
`$(field.$wrapper).find('.control-label').css({...})` (confirmed as a real
miss comparing against a separate audit tool on a real repo: this is
exactly the kind of direct-DOM-styling RULE-014 exists to catch, and the
old substring check silently suppressed it).

The exemption is evaluated per jQuery call, not per line (CodeRabbit review):
matching the safe pattern anywhere on the line used to suppress the whole
line, so a genuinely unsafe call sharing a line with one sanctioned call --
e.g. `$(field.$wrapper).find(...); $($wrapper).hide();` -- went unreported.
Each `$(...)`/`jQuery(...)` match is now checked at its own position.

Ported from frappe-pr-reviewer's js_analyzer.py.
"""

from __future__ import annotations

import re

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_JQUERY_RE = re.compile(r"(?<![\w$])(?:\$|jQuery)\s*\(")
_SAFE_CALL_RE = re.compile(r"(?<![\w$])(?:\$|jQuery)\s*\(\s*(?:\$wrapper|frm\.fields_dict)\b")


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
            jquery_matches = list(_JQUERY_RE.finditer(line))
            if not jquery_matches:
                continue
            if all(_SAFE_CALL_RE.match(line, match.start()) for match in jquery_matches):
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
