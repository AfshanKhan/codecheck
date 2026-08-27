"""RULE-030/RULE-031: flag a comment (or contiguous run of comment lines)
that looks like disabled code rather than an explanation.

RULE-030 (Python) parses the comment's text as a Python statement. RULE-031
(JS) has no parser, so it falls back to "looks like code" regexes
(assignment, function call, control-flow keyword). Both grow the window one
comment line at a time to catch a multi-line statement, up to a line cap.
Both skip common non-code comment conventions (shebang, encoding
declarations, type:/noqa/pragma: directives).
"""

from __future__ import annotations

import ast
import re

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_IGNORE_PREFIXES = ("!", "type:", "noqa", "pragma:", "-*-", "coding:", "eslint", "jshint")
_MAX_BLOCK_LINES = 15


def _looks_like_directive(text: str) -> bool:
    lowered = text.lower()
    return any(lowered.startswith(p) for p in _IGNORE_PREFIXES)


def _is_commented_python_code(text: str, allow_pass_fallback: bool = True) -> bool:
    """text may be one line or several (already '#'-stripped). Uniformly
    indents every line to preserve relative indentation, then tries to parse
    as a statement. allow_pass_fallback=False disables appending a synthetic
    `pass` to a colon-terminated header, used while still growing the window."""
    text = text.strip("\n")
    if not text.strip() or _looks_like_directive(text.lstrip().splitlines()[0]):
        return False
    if allow_pass_fallback and text.rstrip().endswith(":"):
        text = text.rstrip() + "\n pass"
    indented = "\n".join("        " + line for line in text.splitlines())
    try:
        tree = ast.parse(f"def _dummy():\n    while True:\n{indented}")
    except SyntaxError:
        return False
    body = tree.body[0].body[0].body  # def -> while -> its body
    if not body:
        return False
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
_JS_LINE_START_RE = re.compile(
    r"^\s*(?:if|else|for|while|function|switch|try|catch|const|let|var|return|throw)\b|"
    r"^\s*[a-zA-Z_$][\w$]*(?:\.[a-zA-Z_$][\w$]*)*\s*\(|"
    r"^\s*[a-zA-Z_$][\w$]*(?:\.[a-zA-Z_$][\w$]*)*\s*=[^=]"
)
_BRACKETS = {"(": ")", "{": "}", "[": "]"}


def _is_commented_js_code(text: str) -> bool:
    text = text.strip()
    if not text or _looks_like_directive(text):
        return False
    return any(pattern.match(text) for pattern in _JS_CODE_PATTERNS)


def _js_bracket_balance(text: str) -> int:
    """Crude open-minus-close bracket count (doesn't account for brackets
    inside string/regex literals) -- only used to decide when to stop
    growing a candidate block."""
    balance = 0
    for ch in text:
        if ch in _BRACKETS:
            balance += 1
        elif ch in _BRACKETS.values():
            balance -= 1
    return balance


_PY_CONTINUATION_KEYWORDS = ("elif", "else", "except", "finally")


def _looks_like_continuation(text: str) -> bool:
    """True if text opens with elif/else/except/finally -- a clause that can
    only continue a preceding if/try, never parse standalone."""
    stripped = text.lstrip()
    for kw in _PY_CONTINUATION_KEYWORDS:
        if stripped == kw or stripped.startswith((kw + " ", kw + ":")):
            return True
    return False


def _dehash(line: str, prefix: str) -> str:
    stripped = line.lstrip()
    rest = stripped[len(prefix) :]
    if rest.startswith(" "):
        rest = rest[1:]
    return rest


def _make_finding(check_id: str, title: str, severity: Severity, explanation: str,
                   file_path: str, start: int, end: int) -> Finding:
    return Finding(
        check_id=check_id,
        tier="rules",
        source="house",
        severity=severity,
        title=title,
        explanation=explanation,
        file=file_path,
        line_start=start,
        line_end=end,
    )


class CommentedOutPythonCodeCheck(HouseCheck):
    check_id = "RULE-030"
    title = "Comment looks like commented-out code"
    severity = Severity.LOW

    _explanation = (
        "This comment parses as a valid Python statement, not prose -- it looks like "
        "commented-out code rather than an explanation. If it's worth keeping, git "
        "history already has it; if not, remove it to keep the file readable."
    )

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".py"):
            return []
        lines = content.splitlines()
        findings = []
        i = 0
        n = len(lines)
        while i < n:
            # A blank `#` line is never itself the start of a new candidate.
            if not lines[i].strip().startswith("#") or not _dehash(lines[i], "#").strip():
                i += 1
                continue
            matched_end = None
            block: list[str] = []
            j = i
            while j < n and (j - i) < _MAX_BLOCK_LINES and lines[j].strip().startswith("#"):
                block.append(_dehash(lines[j], "#"))
                if _is_commented_python_code("\n".join(block), allow_pass_fallback=False):
                    # If the next comment line is an elif/else/except/finally
                    # continuation, keep growing instead of finalizing here.
                    # A blank line may separate the suite from its clause.
                    following = j + 1
                    while (
                        following < n
                        and (following - i) < _MAX_BLOCK_LINES
                        and lines[following].strip().startswith("#")
                        and not _dehash(lines[following], "#").strip()
                    ):
                        following += 1
                    if (
                        following < n
                        and (following - i) < _MAX_BLOCK_LINES
                        and lines[following].strip().startswith("#")
                        and _looks_like_continuation(_dehash(lines[following], "#"))
                    ):
                        j = following
                        continue
                    matched_end = j
                    break
                j += 1
            else:
                # No match found before running out of lines/hitting the cap --
                # last resort: a header-only statement still counts.
                if block and _is_commented_python_code("\n".join(block), allow_pass_fallback=True):
                    matched_end = j - 1
            if matched_end is None:
                i += 1
                continue
            start_line, end_line = i + 1, matched_end + 1
            if changed_lines is None or any(
                ln in changed_lines for ln in range(start_line, end_line + 1)
            ):
                findings.append(
                    _make_finding(
                        self.check_id, self.title, self.severity, self._explanation,
                        file_path, start_line, end_line,
                    )
                )
            i = matched_end + 1
        return findings


class CommentedOutJsCodeCheck(HouseCheck):
    check_id = "RULE-031"
    title = "Comment looks like commented-out code"
    severity = Severity.LOW

    _explanation = (
        "This comment looks like commented-out code (an assignment, function "
        "call, or control-flow statement), not an explanation. If it's worth "
        "keeping, git history already has it; if not, remove it to keep the "
        "file readable."
    )

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".js"):
            return []
        lines = content.splitlines()
        findings = []
        i = 0
        n = len(lines)
        while i < n:
            if not lines[i].strip().startswith("//"):
                i += 1
                continue
            first = _dehash(lines[i], "//")
            matched_end = None
            first_balance = _js_bracket_balance(first)
            is_open_candidate = bool(_JS_LINE_START_RE.match(first)) and first_balance > 0
            if is_open_candidate:
                # An open multi-line block ("if (ready) {", "frappe.call({", ...)
                # -- pull in following comment lines, tracking bracket
                # balance, until it closes or the line cap is hit.
                balance = first_balance
                j = i
                while balance > 0 and j + 1 < n and (j + 1 - i) < _MAX_BLOCK_LINES:
                    j += 1
                    if not lines[j].strip().startswith("//"):
                        j -= 1
                        break
                    balance += _js_bracket_balance(_dehash(lines[j], "//"))
                if balance <= 0 and j > i:
                    matched_end = j
            # An open candidate that never closes shouldn't fall back to a
            # single-line match either.
            if matched_end is None and not is_open_candidate and _is_commented_js_code(first):
                matched_end = i
            if matched_end is None:
                i += 1
                continue
            start_line, end_line = i + 1, matched_end + 1
            if changed_lines is None or any(
                ln in changed_lines for ln in range(start_line, end_line + 1)
            ):
                findings.append(
                    _make_finding(
                        self.check_id, self.title, self.severity, self._explanation,
                        file_path, start_line, end_line,
                    )
                )
            i = matched_end + 1
        return findings
