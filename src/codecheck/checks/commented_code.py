"""RULE-030/RULE-031: flag a comment (or contiguous run of comment lines)
that looks like it's actually disabled code rather than an explanation --
dead code left in a comment rots (nobody keeps it in sync with the code
around it) and is what version control is for; if it's worth keeping, it
belongs in git history, not a comment.

RULE-030 (Python) tries to parse the comment's text as a Python statement --
if it parses cleanly as something other than a single bare literal/name
(a plain word, "TODO", "None", a number -- clearly prose, not code), it's
almost certainly commented-out code, not an explanation. RULE-031 (JS) has no
parser available, so it falls back to a smaller set of "looks like code"
regexes (assignment, function call, control-flow keyword).

Both look past a single line: a commented-out multi-line statement (a
multi-line SQL string, a chained `frappe.call({...})`) doesn't parse (or
match) on its own first line alone, so both grow the window one comment line
at a time -- trying the accumulated block after each line -- until it either
parses/matches as a complete statement or a line cap is hit (confirmed real
gap: a commented-out multi-line f-string SQL query in a real audited repo
was invisible to the original single-line-only version of this check, while
a much noisier third-party tool's regex-only approach caught it, at the cost
of a high false-positive rate on ordinary prose comments starting with a
keyword-like word such as "return"). Growing the window only when the
current one *doesn't* already match keeps the common single-line case
exactly as precise as before -- it's tried at window size 1 first, same as
always.

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
_MAX_BLOCK_LINES = 15


def _looks_like_directive(text: str) -> bool:
    lowered = text.lower()
    return any(lowered.startswith(p) for p in _IGNORE_PREFIXES)


def _is_commented_python_code(text: str) -> bool:
    """text may be one line or several (already '#'-stripped, joined with
    real newlines) -- ast.parse doesn't care either way. Uniformly
    indenting every line by the same amount (rather than just the first)
    is safe for a multi-line block: it preserves each line's indentation
    *relative* to the others, which is all Python's own parser cares about,
    and any extra whitespace that lands inside an open string/bracket is
    harmless since we only care whether this parses, not its exact value.
    """
    text = text.strip("\n")
    if not text.strip() or _looks_like_directive(text.lstrip().splitlines()[0]):
        return False
    if text.rstrip().endswith(":"):
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
_JS_OPEN_START_RE = re.compile(
    r"^\s*(?:[a-zA-Z_$][\w$]*(?:\.[a-zA-Z_$][\w$]*)*\s*\(|"
    r"(?:if|for|while|function|switch)\b.*\(|"
    r"(?:try|else)\b)\s*[{(]?\s*$"
)
_BRACKETS = {"(": ")", "{": "}", "[": "]"}


def _is_commented_js_code(text: str) -> bool:
    text = text.strip()
    if not text or _looks_like_directive(text):
        return False
    return any(pattern.match(text) for pattern in _JS_CODE_PATTERNS)


def _js_bracket_balance(text: str) -> int:
    """Crude open-minus-close bracket count across the (already //-stripped)
    text -- doesn't account for brackets inside string/regex literals, but
    is only used to decide when to stop growing a candidate block, not to
    decide whether it's code at all, so an occasional miscount just means
    growing the window one line further or less than ideal, not a false
    positive/negative on its own.
    """
    balance = 0
    for ch in text:
        if ch in _BRACKETS:
            balance += 1
        elif ch in _BRACKETS.values():
            balance -= 1
    return balance


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
            if not lines[i].strip().startswith("#"):
                i += 1
                continue
            matched_end = None
            block: list[str] = []
            j = i
            while j < n and (j - i) < _MAX_BLOCK_LINES and lines[j].strip().startswith("#"):
                block.append(_dehash(lines[j], "#"))
                if _is_commented_python_code("\n".join(block)):
                    matched_end = j
                    break
                j += 1
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
            if _is_commented_js_code(first):
                matched_end = i
            elif _JS_OPEN_START_RE.match(first.strip()):
                # Looks like the start of a multi-line call/control-flow
                # statement (ends in an unclosed bracket) -- keep pulling
                # in following comment lines and tracking bracket balance
                # until it closes back out, up to the line cap.
                balance = _js_bracket_balance(first)
                j = i
                while balance > 0 and j + 1 < n and (j + 1 - i) < _MAX_BLOCK_LINES:
                    j += 1
                    if not lines[j].strip().startswith("//"):
                        j -= 1
                        break
                    balance += _js_bracket_balance(_dehash(lines[j], "//"))
                if balance <= 0 and j > i:
                    matched_end = j
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
