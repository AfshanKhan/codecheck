"""RULE-011: flag inline `style=` CSS attributes in Frappe client-script JS --
inline styles bypass the app's theme/CSS and don't respond to dark mode or
theme overrides.

Ported from frappe-pr-reviewer's js_analyzer.py.
"""

from __future__ import annotations

import re

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_INLINE_STYLE_RE = re.compile(r"""style\s*=\s*['"]""", re.IGNORECASE)


class JsInlineStyleCheck(HouseCheck):
    check_id = "RULE-011"
    title = "Inline style= attribute"
    severity = Severity.LOW

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".js"):
            return []

        findings = []
        for lineno, line in enumerate(content.splitlines(), start=1):
            if not _INLINE_STYLE_RE.search(line):
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
                        "Inline style= attributes bypass the app's stylesheet and don't "
                        "respond to theme changes (e.g. dark mode). Use a CSS class instead."
                    ),
                    file=file_path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )
        return findings
