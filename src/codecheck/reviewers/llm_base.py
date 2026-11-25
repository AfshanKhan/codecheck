"""Shared execution loop and base class for LLM tiers (cloud and local).

Encapsulates file reading, line count bounds, time budgeting, overload circuit
breaking, diff-scope filtering, and progress notifications.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Callable
from pathlib import Path

from codecheck.diff import read_file_content
from codecheck.models import Finding, ReviewTarget
from codecheck.reviewers.base import Reviewer

OVERLOAD_STOP_AFTER = 3
OVERLOAD_STOP_REASON = "provider overloaded (HTTP 503/529) on several files in a row, not reviewed"


_DIFF_LINE_TOLERANCE = 2


def within_diff_scope(target: ReviewTarget, finding: Finding) -> bool:
    """In diff mode, only findings on (or near) an actually-changed line are
    kept -- enforced programmatically, not just via the prompt."""
    if target.changed_lines is None:
        return True
    end = finding.line_end or finding.line_start
    return any(
        line in target.changed_lines
        for line in range(finding.line_start - _DIFF_LINE_TOLERANCE, end + _DIFF_LINE_TOLERANCE + 1)
    )


class BaseLlmReviewer(Reviewer):
    """Base class providing the common review iteration loop for all LLM reviewers."""

    def __init__(self, config) -> None:
        self.config = config
        self.skipped_files: list[tuple[str, str]] = []
        self.failed_files: list[tuple[str, str]] = []
        self.reviewed_count: int = 0
        self._overloaded: bool = False
        self._daily_quota: bool = False
        self.sent_paths: list[str] = []

    @abstractmethod
    def _get_client(self):
        """Returns the HTTP client used for completions."""
        ...

    @abstractmethod
    def _review_file(self, client, target: ReviewTarget, content: str) -> tuple[list[dict], str | None]:
        """Review a single file content, returning (raw_findings, error_message)."""
        ...

    @abstractmethod
    def _finding_from_raw(self, raw: dict, file_path: str, counter: int) -> Finding:
        """Map raw dictionary from tool call / response to a Finding instance."""
        ...

    def review(
        self,
        targets: list[ReviewTarget],
        repo_path: Path,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> list[Finding]:
        self.skipped_files = []
        self.failed_files = []
        self.reviewed_count = 0
        self._daily_quota = False
        self._overloaded = False
        overload_streak, stop_reason = 0, None
        client = self._get_client()
        findings: list[Finding] = []
        finding_counter = 0

        budget = getattr(self.config, "time_budget_seconds", None)
        deadline = (time.monotonic() + budget) if budget is not None else None

        for target in targets:
            if target.status == "deleted":
                continue
            if not stop_reason and deadline and time.monotonic() > deadline:
                stop_reason = "time budget reached, not reviewed"
            if stop_reason:
                reason = stop_reason
                self.skipped_files.append((target.path, reason))
                if on_progress:
                    on_progress(target.path, f"skipped: {reason}")
                continue

            content = read_file_content(repo_path, target)
            if content is None:
                self.skipped_files.append((target.path, "could not read file content"))
                if on_progress:
                    on_progress(target.path, "skipped: could not read file content")
                continue

            line_count = content.count("\n") + 1
            if line_count > self.config.max_file_lines:
                reason = f"file too large ({line_count} lines > {self.config.max_file_lines})"
                self.skipped_files.append((target.path, reason))
                if on_progress:
                    on_progress(target.path, f"skipped: {reason}")
                continue

            self._overloaded = False
            raw_findings, error = self._review_file(client, target, content)
            overload_streak = overload_streak + 1 if self._overloaded else 0

            if error:
                self.skipped_files.append((target.path, error))
                self.failed_files.append((target.path, error))
                if getattr(self, "_daily_quota", False):
                    stop_reason = "daily request quota for this API key is used up, not reviewed"
                elif overload_streak >= OVERLOAD_STOP_AFTER:
                    stop_reason = OVERLOAD_STOP_REASON
                if on_progress:
                    on_progress(target.path, f"skipped: {error}")
                continue

            self.reviewed_count += 1
            file_finding_count = 0
            for raw in raw_findings:
                finding_counter += 1
                finding = self._finding_from_raw(raw, target.path, finding_counter)
                if within_diff_scope(target, finding):
                    findings.append(finding)
                    file_finding_count += 1
            if on_progress:
                noun = "finding" if file_finding_count == 1 else "findings"
                on_progress(target.path, f"{file_finding_count} {noun}")

        return findings
