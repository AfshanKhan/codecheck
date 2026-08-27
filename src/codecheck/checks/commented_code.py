"""RULE-030/RULE-031: flag a comment line that looks like it's actually
disabled code rather than an explanation -- dead code left in a comment
rots (nobody keeps it in sync with the code around it) and is what version
control is for; if it's worth keeping, it belongs in git history, not a
comment.

RULE-030 (Python) tries to parse the comment's text as a Python statement --
if it parses cleanly as something other than a single bare literal/name
(a plain word, "TODO", "None", a number -- clearly prose, not code), it's
almost certainly commented-out code, not an explanation. RULE-031 (JS) has no
parser available, so it falls back to a smaller set of "looks like code"
regexes (assignment, function call, control-flow keyword) -- necessarily
looser, so it only fires on single-line `//` comments, not block comments,
to keep the false-positive rate down.

Both skip common non-code comment conventions (shebang, encoding declarations,
type: / noqa / pragma: directives) that would otherwise round-trip through
the parser as valid-looking statements.
"""

from __future__ import annotations

import ast
import re

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_IGNORE_PREFIXES = ("!", "type:", "noqa", "pragma:", "-*-", "coding:", "eslint", "jshint")


def _looks_like_directive(text: str) -> bool:
    lowered = text.lower()
    return any(lowered.startswith(p) for p in _IGNORE_PREFIXES)


def _is_commented_python_code(text: str) -> bool:
    text = text.strip()
    if not text or _looks_like_directive(text):
        return False
    candidate = text + " pass" if text.endswith(":") else text
    try:
        tree = ast.parse(f"def _dummy():\n    while True:\n        {candidate}")
    except SyntaxError:
        return False
    body = tree.body[0].body[0].body  # def -> while -> its body
    if len(body) == 1 and isinstance(body[0], ast.Expr):
        value = body[0].value
        if isinstance(value, (ast.Constant, ast.Name)):
            return False  # a bare word/literal/number is prose, not code
    return True


_JS_CODE_PATTERNS = (
    re.compile(r"^\s*(if|else|for|while|function|return|const|let|var|try|catch|throw|switch|case|break|continue|import|export)\b"),
    re.compile(r"^\s*[a-zA-Z_$][\w$]*(?:\.[a-zA-Z_$][\w$]*)*\s*=[^=]"),  # assignment
    re.compile(r"^\s*[a-zA-Z_$][\w$]*(?:\.[a-zA-Z_$][\w$]*)*\s*\(.*\)\s*;?\s*$"),  # call
)


def _is_commented_js_code(text: str) -> bool:
    text = text.strip()
    if not text or _looks_like_directive(text):
        return False
    return any(pattern.match(text) for pattern in _JS_CODE_PATTERNS)


class CommentedOutPythonCodeCheck(HouseCheck):
    check_id = "RULE-030"
    title = "Comment looks like commented-out code"
    severity = Severity.LOW

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".py"):
            return []
        findings = []
        for lineno, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith("#"):
                continue
            if not _is_commented_python_code(stripped[1:]):
                continue
            if changed_lines is not None and lineno not in changed_lines:
                continue
            findings.append(self._finding(file_path, lineno))
        return findings

    def _finding(self, file_path: str, lineno: int) -> Finding:
        return Finding(
            check_id=self.check_id,
            tier="rules",
            source="house",
            severity=self.severity,
            title=self.title,
            explanation=(
                "This comment parses as a valid Python statement, not prose -- it looks like "
                "commented-out code rather than an explanation. If it's worth keeping, git "
                "history already has it; if not, remove it to keep the file readable."
            ),
            file=file_path,
            line_start=lineno,
            line_end=lineno,
        )


class CommentedOutJsCodeCheck(HouseCheck):
    check_id = "RULE-031"
    title = "Comment looks like commented-out code"
    severity = Severity.LOW

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".js"):
            return []
        findings = []
        for lineno, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith("//"):
                continue
            if not _is_commented_js_code(stripped[2:]):
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
                        "This comment looks like commented-out code (an assignment, function "
                        "call, or control-flow statement), not an explanation. If it's worth "
                        "keeping, git history already has it; if not, remove it to keep the "
                        "file readable."
                    ),
                    file=file_path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )
        return findings
