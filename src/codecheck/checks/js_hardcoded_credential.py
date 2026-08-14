"""RULE-016: flag a JS variable/property assignment whose name looks like a
secret (password, api_key, token, etc.) and whose value is a non-empty,
non-placeholder string literal.

Ported from frappe-pr-reviewer's js_analyzer.py.
"""

from __future__ import annotations

import re

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_SENSITIVE_NAMES = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "token",
    "private_key",
    "credential",
    "client_secret",
)
_PLACEHOLDERS = {"", "your_token_here", "changeme", "xxx"}

_ASSIGNMENT_RE = re.compile(
    r"""(?:const|let|var)?\s*([a-zA-Z_$][\w$]*)\s*[:=]\s*['"]([^'"]*)['"]"""
)


class JsHardcodedCredentialCheck(HouseCheck):
    check_id = "RULE-016"
    title = "Possible hardcoded credential"
    severity = Severity.HIGH

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".js"):
            return []

        findings = []
        for lineno, line in enumerate(content.splitlines(), start=1):
            match = _ASSIGNMENT_RE.search(line)
            if not match:
                continue
            name, value = match.group(1), match.group(2)
            if value.strip().lower() in _PLACEHOLDERS:
                continue
            if not any(sensitive in name.lower() for sensitive in _SENSITIVE_NAMES):
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
                        "This variable name suggests a secret (password/token/key), and it's "
                        "assigned a hardcoded string literal. Client-side JS is always visible "
                        "to end users -- never put real secrets here; load them server-side."
                    ),
                    file=file_path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )
        return findings
