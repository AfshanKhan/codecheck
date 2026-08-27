"""RULE-028: flag direct DOM manipulation in a Frappe client script --
document.getElementById/querySelector, or setting .innerHTML -- reaching
past the frm/dialog APIs the framework expects scripts to use. Distinct from
RULE-014 (js_jquery_dom.py), which covers the jQuery-specific spelling of the
same underlying concern ($(...)/jQuery(...)); this covers the vanilla-JS DOM
APIs a script might use instead of (or alongside) jQuery.
"""

from __future__ import annotations

import re

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_DOM_RE = re.compile(r"document\.getElementById|document\.querySelector|\.innerHTML\b")


class JsDirectDomCheck(HouseCheck):
    check_id = "RULE-028"
    title = "Direct DOM manipulation in a client script"
    severity = Severity.LOW

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".js"):
            return []

        findings = []
        for lineno, line in enumerate(content.splitlines(), start=1):
            if not _DOM_RE.search(line):
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
                        "Direct DOM manipulation reaches outside the frm/dialog APIs Frappe "
                        "scripts are expected to use. Prefer frm.set_df_property(), "
                        "frm.get_field(), or a dialog field instead, if one covers this case."
                    ),
                    file=file_path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )
        return findings
