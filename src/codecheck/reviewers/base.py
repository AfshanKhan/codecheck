"""Reviewer interface implemented by every tier (rules engine, local LLM, cloud LLM)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from codecheck.models import Finding, ReviewTarget


class Reviewer(ABC):
    tier: ClassVar[str]
    name: ClassVar[str]

    @abstractmethod
    def is_available(self, repo_path: Path) -> tuple[bool, str | None]:
        """Return (available, reason_if_not). Called before review()."""

    @abstractmethod
    def review(self, targets: list[ReviewTarget], repo_path: Path) -> list[Finding]:
        ...
