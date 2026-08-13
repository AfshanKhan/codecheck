"""Core data model: Finding, Severity, ReviewReport."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    def __ge__(self, other: "Severity") -> bool:
        return self.rank >= other.rank

    def __gt__(self, other: "Severity") -> bool:
        return self.rank > other.rank

    def __le__(self, other: "Severity") -> bool:
        return self.rank <= other.rank

    def __lt__(self, other: "Severity") -> bool:
        return self.rank < other.rank

    @classmethod
    def parse(cls, value: str | None) -> "Severity":
        """Case-insensitive parse with a MEDIUM fallback for anything unrecognized —
        LLM output doesn't reliably match a JSON schema's enum casing/values.
        """
        if not value:
            return cls.MEDIUM
        try:
            return cls(value.strip().lower())
        except ValueError:
            return cls.MEDIUM


_SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}

SEVERITY_COLOR = {
    Severity.INFO: "dim",
    Severity.LOW: "blue",
    Severity.MEDIUM: "yellow",
    Severity.HIGH: "red",
    Severity.CRITICAL: "bold red",
}


@dataclass
class ReviewTarget:
    """A file to review, whether it came from a diff or a whole-repo scan.

    `changed_lines=None` means every line in the file is in scope (whole-repo
    audit mode); a concrete set means only those lines are in scope (diff mode).
    """

    path: str
    status: str  # "added" | "modified" | "deleted" | "renamed" | "scanned"
    diff_text: str = ""
    changed_lines: set[int] | None = field(default_factory=set)
    old_path: str | None = None


@dataclass
class Finding:
    check_id: str
    tier: str
    source: str
    severity: Severity
    title: str
    explanation: str
    file: str
    line_start: int
    line_end: int | None = None
    suggestion: str | None = None
    raw: dict | None = None

    def to_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "tier": self.tier,
            "source": self.source,
            "severity": self.severity.value,
            "title": self.title,
            "explanation": self.explanation,
            "file": self.file,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "suggestion": self.suggestion,
            "raw": self.raw,
        }


@dataclass
class ReviewReport:
    repo_path: str
    mode: str  # "diff" | "audit"
    base_ref: str | None
    head_ref: str | None
    generated_at: datetime
    tiers_run: list[str]
    findings: list[Finding] = field(default_factory=list)
    files_reviewed: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    skipped: list[str] = field(default_factory=list)
    # sha256 of each reviewed file's content at review time -- lets a later
    # --resume-from run confirm a file hasn't changed before reusing its
    # prior result, rather than trusting the path alone. See codecheck.resume.
    file_hashes: dict[str, str] = field(default_factory=dict)

    def findings_at_or_above(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity >= severity]

    def by_file(self) -> dict[str, list[Finding]]:
        grouped: dict[str, list[Finding]] = {}
        for f in self.findings:
            grouped.setdefault(f.file, []).append(f)
        return grouped

    def counts_by_severity(self) -> dict[Severity, int]:
        counts = {s: 0 for s in Severity}
        for f in self.findings:
            counts[f.severity] += 1
        return counts

    def to_dict(self) -> dict:
        return {
            "repo_path": self.repo_path,
            "mode": self.mode,
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "generated_at": self.generated_at.isoformat(),
            "tiers_run": self.tiers_run,
            "duration_seconds": self.duration_seconds,
            "files_reviewed": self.files_reviewed,
            "skipped": self.skipped,
            "file_hashes": self.file_hashes,
            "counts_by_severity": {
                s.value: c for s, c in self.counts_by_severity().items()
            },
            "findings": [f.to_dict() for f in self.findings],
        }
