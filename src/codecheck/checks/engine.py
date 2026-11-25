"""Consolidated single-pass AST check engine for house rules.

Parses Python source code once per file, walks the AST in a single pass,
and dispatches nodes to registered AstRule matchers with built-in changed_lines scoping.
"""

from __future__ import annotations

import ast
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Sequence

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_LOOP_NODE_TYPES = (
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ListComp,
    ast.DictComp,
    ast.SetComp,
    ast.GeneratorExp,
)


@dataclass
class AstContext:
    """Read-only context provided to AstRule matchers during AST traversal."""

    file_path: str
    changed_lines: set[int] | None
    tree: ast.AST
    loop_depth: int = 0

    def is_line_in_scope(self, lineno: int) -> bool:
        return self.changed_lines is None or lineno in self.changed_lines

    def is_range_in_scope(self, start: int, end: int) -> bool:
        if self.changed_lines is None:
            return True
        return not self.changed_lines.isdisjoint(range(start, end + 1))


@dataclass
class Violation:
    """Represents a rule match on an AST node."""

    node: ast.AST
    explanation: str
    severity: Severity | None = None
    title: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    scope_mode: str | None = None  # None -> inherits from rule


class AstRule(ABC):
    """Lightweight rule matcher. Declares AST node types of interest."""

    check_id: ClassVar[str]
    title: ClassVar[str]
    severity: ClassVar[Severity]
    node_types: ClassVar[tuple[type[ast.AST], ...]]
    scope_mode: ClassVar[str] = "start_line"  # "start_line" or "any_in_range"

    @abstractmethod
    def match(self, node: Any, ctx: AstContext) -> Violation | list[Violation] | None:
        """Inspect a subscribed AST node. Return Violation(s) if flagged."""
        ...


class NoBareExceptRule(AstRule):
    """RULE-001: bare except: clause."""

    check_id = "RULE-001"
    title = "Bare except clause"
    severity = Severity.MEDIUM
    node_types = (ast.ExceptHandler,)

    def match(self, node: ast.ExceptHandler, ctx: AstContext) -> Violation | None:
        if node.type is None:
            return Violation(
                node=node,
                line_start=node.lineno,
                line_end=node.lineno,
                explanation=(
                    "Bare `except:` catches all exceptions, including KeyboardInterrupt "
                    "and SystemExit, and can hide real bugs. Catch a specific exception type instead."
                ),
            )
        return None


class SilentExceptionRule(AstRule):
    """RULE-008: typed except with empty body (pass or docstring)."""

    check_id = "RULE-008"
    title = "Exception caught and silently discarded"
    severity = Severity.MEDIUM
    node_types = (ast.ExceptHandler,)

    @staticmethod
    def _is_silent_body(body: list[ast.stmt]) -> bool:
        if len(body) != 1:
            return False
        stmt = body[0]
        if isinstance(stmt, ast.Pass):
            return True
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(
            stmt.value.value, str
        ):
            return True
        return False

    def match(self, node: ast.ExceptHandler, ctx: AstContext) -> Violation | None:
        if node.type is not None and self._is_silent_body(node.body):
            return Violation(
                node=node,
                line_start=node.lineno,
                line_end=node.lineno,
                explanation=(
                    "This except clause catches a specific exception but does nothing "
                    "with it, silently discarding the error. If it's genuinely safe to "
                    "ignore, log it (frappe.log_error()) so it's visible when debugging; "
                    "otherwise handle it."
                ),
            )
        return None


class LeftoverPrintRule(AstRule):
    """RULE-007: leftover print() calls."""

    check_id = "RULE-007"
    title = "Leftover print() call"
    severity = Severity.LOW
    node_types = (ast.Call,)

    def match(self, node: ast.Call, ctx: AstContext) -> Violation | None:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "print":
            return Violation(
                node=node,
                line_start=node.lineno,
                line_end=node.lineno,
                explanation=(
                    "print() output goes to server stdout, not the site's log, and is "
                    "almost always leftover debug output. Use frappe.logger().info(...) "
                    "or frappe.log_error(...) instead, or remove it."
                ),
            )
        return None


class NoManualCommitRule(AstRule):
    """RULE-005: manual frappe.db.commit()."""

    check_id = "RULE-005"
    title = "Manual frappe.db.commit() call"
    severity = Severity.MEDIUM
    node_types = (ast.Call,)

    @staticmethod
    def _is_db_commit(node: ast.Call) -> bool:
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "commit"):
            return False
        val = func.value
        if isinstance(val, ast.Name) and val.id == "db":
            return True
        if isinstance(val, ast.Attribute) and val.attr == "db":
            return True
        return False

    def match(self, node: ast.Call, ctx: AstContext) -> Violation | None:
        if self._is_db_commit(node):
            return Violation(
                node=node,
                line_start=node.lineno,
                line_end=node.lineno,
                explanation=(
                    "Frappe auto-commits at the end of a successful request; a manual "
                    "db.commit() mid-request can commit a partial transaction if later "
                    "code in the same request raises. Remove it unless there's a specific, "
                    "documented reason a partial commit here is safe (e.g. a long-running "
                    "background job that intentionally checkpoints)."
                ),
            )
        return None


class NoSqlStringFormatRule(AstRule):
    """RULE-002: frappe.db.sql() with string formatting."""

    check_id = "RULE-002"
    title = "frappe.db.sql() built with string formatting"
    severity = Severity.HIGH
    node_types = (ast.Call,)

    @staticmethod
    def _is_frappe_db_sql(node: ast.Call) -> bool:
        func = node.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "sql"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "db"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "frappe"
        )

    @staticmethod
    def _uses_escape(node: ast.Call) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and "escape" in sub.attr.lower():
                return True
            if isinstance(sub, ast.Name) and "escape" in sub.id.lower():
                return True
        return False

    @staticmethod
    def _query_arg_is_unsafe(arg: ast.expr) -> bool:
        if isinstance(arg, ast.JoinedStr):
            return True
        if isinstance(arg, ast.BinOp) and isinstance(arg.op, (ast.Mod, ast.Add)):
            return True
        if (
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Attribute)
            and arg.func.attr == "format"
        ):
            return True
        return False

    def match(self, node: ast.Call, ctx: AstContext) -> Violation | None:
        if not self._is_frappe_db_sql(node):
            return None
        if not node.args or not self._query_arg_is_unsafe(node.args[0]):
            return None
        escaped = self._uses_escape(node)
        return Violation(
            node=node,
            severity=Severity.MEDIUM if escaped else self.severity,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            explanation=(
                (
                    "frappe.db.sql() query is built with string formatting, but "
                    "frappe.db.escape() is used on the interpolated value(s) -- lower "
                    "risk than an unsanitized value, but still prefer parameterized "
                    "queries: frappe.db.sql(query, params)."
                )
                if escaped
                else (
                    "frappe.db.sql() query is built with string formatting "
                    "(f-string, %, .format(), or +), which risks SQL injection. "
                    "Use parameterized queries: frappe.db.sql(query, params)."
                )
            ),
        )


class MissingTranslationRule(AstRule):
    """RULE-006: raw string in frappe.throw/msgprint."""

    check_id = "RULE-006"
    title = "User-facing message not wrapped in _()"
    severity = Severity.LOW
    node_types = (ast.Call,)

    _FLAGGED_NAMES = {"throw", "msgprint"}

    @classmethod
    def _is_flagged_call(cls, node: ast.Call) -> bool:
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else (func.id if isinstance(func, ast.Name) else "")
        )
        return name in cls._FLAGGED_NAMES

    @staticmethod
    def _is_raw_message(arg: ast.expr) -> bool:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return True
        if isinstance(arg, ast.JoinedStr):
            return True
        if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
            return True
        return False

    @staticmethod
    def _call_name(node: ast.Call) -> str:
        func = node.func
        if isinstance(func, ast.Attribute):
            return f"frappe.{func.attr}"
        if isinstance(func, ast.Name):
            return func.id
        return "frappe.throw"

    def match(self, node: ast.Call, ctx: AstContext) -> Violation | None:
        if not self._is_flagged_call(node):
            return None
        if not node.args or not self._is_raw_message(node.args[0]):
            return None
        return Violation(
            node=node,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            explanation=(
                "This message is a raw string, not wrapped in _(), so it won't be "
                "translated for non-English users. Wrap it: frappe.throw(_(\"message\"))."
            ),
        )


class NPlusOneQueryRule(AstRule):
    """RULE-004: Frappe DB query inside loop."""

    check_id = "RULE-004"
    title = "Database call inside a loop (possible N+1 query)"
    severity = Severity.MEDIUM
    node_types = (ast.Call,)

    _FLAGGED_ATTRS = {
        "get_doc",
        "get_all",
        "get_list",
        "get_meta",
        "get_value",
        "get_values",
        "get_single_value",
        "set_value",
        "sql",
        "exists",
        "count",
    }

    @classmethod
    def _is_flagged_call(cls, node: ast.Call) -> bool:
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in cls._FLAGGED_ATTRS:
            return False
        val = func.value
        if isinstance(val, ast.Name):
            return val.id in ("frappe", "db", "self")
        if isinstance(val, ast.Attribute):
            return val.attr in ("db",) or (
                isinstance(val.value, ast.Name) and val.value.id == "frappe"
            )
        return False

    def match(self, node: ast.Call, ctx: AstContext) -> Violation | None:
        if ctx.loop_depth > 0 and self._is_flagged_call(node):
            return Violation(
                node=node,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                explanation=(
                    "This database call runs once per loop iteration instead of "
                    "being batched before the loop. For large datasets this turns "
                    "into many round-trips (N+1 queries) -- consider fetching once "
                    "with a single get_all()/get_list() call and looking up results "
                    "from an in-memory dict inside the loop."
                ),
            )
        return None


class MethodTooLongRule(AstRule):
    """RULE-018: function body > 50 lines."""

    check_id = "RULE-018"
    title = "Function is too long"
    severity = Severity.LOW
    node_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    scope_mode = "any_in_range"

    _MAX_LINES = 50

    def match(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, ctx: AstContext
    ) -> Violation | None:
        end_line = getattr(node, "end_lineno", None) or node.lineno
        length = end_line - node.lineno + 1
        if length > self._MAX_LINES:
            return Violation(
                node=node,
                line_start=node.lineno,
                line_end=node.lineno,
                scope_mode="any_in_range",
                explanation=(
                    f"'{node.name}' is {length} lines long (recommended maximum is "
                    f"{self._MAX_LINES}). Consider extracting helper functions or splitting "
                    "it into smaller units of responsibility."
                ),
            )
        return None


DEFAULT_AST_RULES: list[AstRule] = [
    NoBareExceptRule(),
    NoSqlStringFormatRule(),
    NPlusOneQueryRule(),
    NoManualCommitRule(),
    MissingTranslationRule(),
    LeftoverPrintRule(),
    SilentExceptionRule(),
    MethodTooLongRule(),
]


class AstCheckEngine(HouseCheck):
    """Single-pass engine that parses source once, traverses the AST once,
    and dispatches nodes directly to registered AstRules."""

    check_id = "ENGINE-AST"
    title = "Consolidated AST Check Engine"
    severity = Severity.MEDIUM

    def __init__(self, rules: Sequence[AstRule] | None = None):
        self.rules = list(rules if rules is not None else DEFAULT_AST_RULES)
        self._dispatch: dict[type[ast.AST], list[AstRule]] = {}
        for rule in self.rules:
            for nt in rule.node_types:
                self._dispatch.setdefault(nt, []).append(rule)

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".py"):
            return []
        try:
            tree = ast.parse(content, filename=file_path)
        except SyntaxError:
            return []

        ctx = AstContext(file_path=file_path, changed_lines=changed_lines, tree=tree)
        findings: list[Finding] = []

        stack: list[tuple[ast.AST, int]] = [(tree, 0)]
        while stack:
            node, loop_depth = stack.pop()
            ctx.loop_depth = loop_depth

            rules = self._dispatch.get(type(node))
            if rules:
                for rule in rules:
                    self._run_rule(rule, node, ctx, findings)

            child_depth = loop_depth + 1 if isinstance(node, _LOOP_NODE_TYPES) else loop_depth

            children = list(ast.iter_child_nodes(node))
            for child in reversed(children):
                stack.append((child, child_depth))

        return findings

    def _run_rule(
        self, rule: AstRule, node: ast.AST, ctx: AstContext, findings: list[Finding]
    ) -> None:
        node_lineno = getattr(node, "lineno", None)
        if ctx.changed_lines is not None and rule.scope_mode == "start_line":
            if node_lineno is not None and node_lineno not in ctx.changed_lines:
                return

        result = rule.match(node, ctx)
        if not result:
            return

        violations = [result] if isinstance(result, Violation) else result
        for v in violations:
            v_start = v.line_start if v.line_start is not None else getattr(v.node, "lineno", 1)
            v_end = v.line_end if v.line_end is not None else getattr(v.node, "end_lineno", v_start)

            if ctx.changed_lines is not None:
                scope = v.scope_mode or rule.scope_mode
                if scope == "any_in_range":
                    node_start = getattr(node, "lineno", v_start)
                    node_end = getattr(node, "end_lineno", node_start)
                    if ctx.changed_lines.isdisjoint(range(node_start, node_end + 1)):
                        continue
                else:
                    if v_start not in ctx.changed_lines:
                        continue

            findings.append(
                Finding(
                    check_id=rule.check_id,
                    tier="rules",
                    source="house",
                    severity=v.severity or rule.severity,
                    title=v.title or rule.title,
                    explanation=v.explanation,
                    file=ctx.file_path,
                    line_start=v_start,
                    line_end=v_end,
                )
            )
