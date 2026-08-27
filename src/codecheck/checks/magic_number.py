"""RULE-032: flag a numeric literal used directly in a calculation or
comparison instead of a named constant -- 0, 1, -1, 2, and 100 are exempted
as self-explanatory in context (indices, increments, percentages); anything
else (a discount rate, a batch size, a timeout) reads better as an
UPPERCASE_CONSTANT that documents what the number means and gives future
callers one place to change it.

Assignments to an UPPERCASE name are exempt outright -- that's the constant
declaration itself, not a magic-number use.
"""

from __future__ import annotations

import ast

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_EXEMPT_VALUES = {0, 1, -1, 2, 100}


def _numeric_literal(node: ast.expr) -> tuple[bool, float | int | None]:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        is_num, val = _numeric_literal(node.operand)
        return (is_num, -val) if is_num else (False, None)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(
        node.value, bool
    ):
        return True, node.value
    return False, None


def _is_uppercase_target(node: ast.Assign | ast.AnnAssign) -> bool:
    if isinstance(node, ast.AnnAssign):
        return isinstance(node.target, ast.Name) and node.target.id.isupper()
    # Every target must itself be an uppercase Name -- filtering out
    # non-Name targets *before* checking, rather than requiring each one to
    # pass, let all() vacuously return True on an empty generator for an
    # attribute/subscript target like `settings.limit = total * 4837`,
    # wrongly treating it as a constant declaration and suppressing a real
    # magic number (caught by CodeRabbit review).
    return bool(node.targets) and all(
        isinstance(t, ast.Name) and t.id.isupper() for t in node.targets
    )


class MagicNumberCheck(HouseCheck):
    check_id = "RULE-032"
    title = "Magic number in a calculation or comparison"
    severity = Severity.LOW

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".py"):
            return []
        try:
            tree = ast.parse(content, filename=file_path)
        except SyntaxError:
            return []

        findings: list[Finding] = []
        self._walk(tree, in_constant_assign=False, file_path=file_path,
                   changed_lines=changed_lines, out=findings)
        return findings

    def _walk(
        self,
        node: ast.AST,
        in_constant_assign: bool,
        file_path: str,
        changed_lines: set[int] | None,
        out: list[Finding],
    ) -> None:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            in_constant_assign = _is_uppercase_target(node)

        operands: list[ast.expr] = []
        if isinstance(node, ast.BinOp) and not in_constant_assign:
            operands = [node.left, node.right]
        elif isinstance(node, ast.Compare) and not in_constant_assign:
            operands = [node.left, *node.comparators]

        for operand in operands:
            is_num, value = _numeric_literal(operand)
            if not is_num or value in _EXEMPT_VALUES:
                continue
            lineno = getattr(operand, "lineno", node.lineno)
            if changed_lines is not None and lineno not in changed_lines:
                continue
            out.append(
                Finding(
                    check_id=self.check_id,
                    tier="rules",
                    source="house",
                    severity=self.severity,
                    title=self.title,
                    explanation=(
                        f"Magic number {value!r} used directly here. Consider naming it as an "
                        "UPPERCASE_CONSTANT at module or class level so its meaning is documented "
                        "and there's one place to change it."
                    ),
                    file=file_path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )

        for child in ast.iter_child_nodes(node):
            self._walk(child, in_constant_assign, file_path, changed_lines, out)
