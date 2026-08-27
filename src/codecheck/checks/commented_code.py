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


def _is_commented_python_code(text: str, allow_pass_fallback: bool = True) -> bool:
    """text may be one line or several (already '#'-stripped, joined with
    real newlines) -- ast.parse doesn't care either way. Uniformly
    indenting every line by the same amount (rather than just the first)
    is safe for a multi-line block: it preserves each line's indentation
    *relative* to the others, which is all Python's own parser cares about,
    and any extra whitespace that lands inside an open string/bracket is
    harmless since we only care whether this parses, not its exact value.

    `allow_pass_fallback=False` disables the "append a synthetic pass to a
    colon-terminated header" shortcut -- the caller uses this while it's
    still growing the window, so a genuinely present indented suite in the
    following comment line(s) gets a chance to complete the statement for
    real, rather than the header being accepted as "done" on its own and
    its actual body ending up as a separate, fragmented finding (or missed
    outright if the body isn't independently parseable on its own -- caught
    by CodeRabbit review). The fallback is still used, as a last resort,
    once growing is exhausted (see check_file below).
    """
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
    r"^\s*(?:if|else|for|while|function|switch|try|catch|const|let|var)\b|"
    r"^\s*[a-zA-Z_$][\w$]*(?:\.[a-zA-Z_$][\w$]*)*\s*\("
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


_PY_CONTINUATION_KEYWORDS = ("elif", "else", "except", "finally")


def _looks_like_continuation(text: str) -> bool:
    """True if `text` (one already-'#'-stripped comment line) opens with
    elif/else/except/finally -- a clause that can only ever appear as the
    continuation of a *preceding* if/try statement, never parse as a
    complete unit on its own. Requires the keyword to actually end there
    (followed by nothing, a space, or a colon) so this doesn't misfire on
    a variable named e.g. "elsewhere".
    """
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
            if not lines[i].strip().startswith("#"):
                i += 1
                continue
            matched_end = None
            block: list[str] = []
            j = i
            while j < n and (j - i) < _MAX_BLOCK_LINES and lines[j].strip().startswith("#"):
                block.append(_dehash(lines[j], "#"))
                # No pass-fallback while growing -- a colon-terminated header
                # should only be accepted "as is" once there's genuinely no
                # more comment lines to pull in as its real body.
                if _is_commented_python_code("\n".join(block), allow_pass_fallback=False):
                    # A complete, valid statement -- but if the very next
                    # comment line is an elif/else/except/finally clause,
                    # it can only belong to *this* statement (those never
                    # parse standalone), so keep growing instead of
                    # finalizing here. Otherwise a commented if/else ends
                    # up reported as two fragments with the else/elif
                    # header dropped entirely, since "else:" alone is a
                    # SyntaxError with nothing before it to attach to
                    # (caught by CodeRabbit review). A blank `#` line can
                    # sit between the suite and its continuation clause (a
                    # deliberate visual separator) -- skip past any of
                    # those first, rather than only ever peeking at the
                    # very next line, so the continuation is still found
                    # (also caught by CodeRabbit review, on the fix above).
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
                # Ran out of comment lines (or hit the cap) without a real
                # match -- last resort: a header-only disabled statement
                # (`# if x:` with no comment body ever shown) still counts.
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
                # An open multi-line block ("if (ready) {", "frappe.call({",
                # ...) -- checked *before* the generic single-line patterns
                # below, not after: those matched on the keyword/call prefix
                # alone regardless of whether the line's own brackets were
                # balanced, so an open header got accepted as its own
                # complete single-line finding and its real body (or closing
                # brace) ended up as a separate, fragmented finding instead
                # of one combined range (caught by CodeRabbit review). Keep
                # pulling in following comment lines and tracking bracket
                # balance until it closes back out, up to the line cap.
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
            # A candidate identified as the start of a multi-line block that
            # never actually closes (ran off into prose, or hit the line
            # cap) shouldn't fall back to a single-line match either -- the
            # keyword-prefix pattern below matches "const"/"if"/... alone
            # regardless of what follows, so without this guard an unclosed
            # candidate was still reported as its own (incomplete, partial)
            # single-line finding, inconsistent with an unclosed
            # identifier-call candidate ("frappe.call({" with no closing
            # paren), which already correctly matched nothing at all (caught
            # by CodeRabbit review).
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
