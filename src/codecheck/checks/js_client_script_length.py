"""RULE-029: flag a client script (.js) longer than 200 lines -- a lean
client script is easier to review and less likely to be hiding logic that
belongs on the server instead. File-level, like RULE-018's function-length
check but for a whole script rather than one function.
"""

from __future__ import annotations

from codecheck.checks.base import HouseCheck
from codecheck.models import Finding, Severity

_MAX_LINES = 200


class JsClientScriptLengthCheck(HouseCheck):
    check_id = "RULE-029"
    title = "Client script is too long"
    severity = Severity.LOW

    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        if not file_path.endswith(".js"):
            return []
        lines = content.splitlines()
        length = len(lines)
        if length <= _MAX_LINES:
            return []
        # Whole-file concern -- there's no single "offending line," so this
        # is only worth reporting if the diff touches the file at all
        # (any changed line counts, not just line 1).
        if changed_lines is not None and not changed_lines:
            return []
        return [
            Finding(
                check_id=self.check_id,
                tier="rules",
                source="house",
                severity=self.severity,
                title=self.title,
                explanation=(
                    f"This client script is {length} lines long (recommended maximum is "
                    f"{_MAX_LINES}). Consider moving complex logic to the server side or a "
                    "helper JS module."
                ),
                file=file_path,
                line_start=1,
                line_end=1,
            )
        ]
