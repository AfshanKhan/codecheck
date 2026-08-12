"""House rule check interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from codecheck.models import Finding, Severity


class HouseCheck(ABC):
    check_id: ClassVar[str]
    title: ClassVar[str]
    severity: ClassVar[Severity]

    @abstractmethod
    def check_file(
        self, file_path: str, content: str, changed_lines: set[int] | None
    ) -> list[Finding]:
        """Return findings for this file.

        changed_lines restricts findings to those line numbers; None means every
        line in the file is in scope (whole-repo audit mode).
        """
