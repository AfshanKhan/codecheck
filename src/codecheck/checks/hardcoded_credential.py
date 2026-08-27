"""RULE-009: flag a variable assignment where the name looks like a secret
(password, api_key, token, etc.) and the value is a non-empty,
non-placeholder string literal."""

from __future__ import annotations

import ast

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_SENSITIVE_NAMES = {
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
    "credentials",
    "client_secret",
    "db_password",
    "database_password",
}
_PLACEHOLDERS = {"", "your_token_here", "changeme", "xxx"}


def _target_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [t.id for t in targets if isinstance(t, ast.Name)]


def _looks_sensitive(name: str) -> bool:
    lowered = name.lower()
    return any(sensitive in lowered for sensitive in _SENSITIVE_NAMES)


class HardcodedCredentialCheck(HouseCheck):
    check_id = "RULE-009"
    title = "Possible hardcoded credential"
    severity = Severity.HIGH

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".py"):
            return []
        try:
            tree = ast.parse(content, filename=file_path)
        except SyntaxError:
            return []

        findings = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            if value.value.strip().lower() in _PLACEHOLDERS:
                continue
            if not any(_looks_sensitive(name) for name in _target_names(node)):
                continue
            if changed_lines is not None and node.lineno not in changed_lines:
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
                        "assigned a hardcoded string literal. Load it from an environment "
                        "variable, site_config.json, or another secrets store instead."
                    ),
                    file=file_path,
                    line_start=node.lineno,
                    line_end=node.lineno,
                )
            )
        return findings
