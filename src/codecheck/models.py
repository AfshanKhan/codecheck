"""Core data model: Finding, Severity, ReviewReport."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
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
        """Case-insensitive parse with a MEDIUM fallback for anything unrecognized."""
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

# Per-severity weight for ReviewReport.compliance_percentage().
# _MAX_FILE_WEIGHT (== the CRITICAL weight) is the per-file penalty cap.
SEVERITY_WEIGHT = {
    Severity.CRITICAL: 4.0,
    Severity.HIGH: 3.0,
    Severity.MEDIUM: 2.0,
    Severity.LOW: 1.0,
    Severity.INFO: 0.5,
}
_MAX_FILE_WEIGHT = SEVERITY_WEIGHT[Severity.CRITICAL]


@dataclass
class ReviewTarget:
    """A file to review. changed_lines=None means every line is in scope
    (audit mode); a concrete set scopes to those lines (diff mode)."""

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

    @classmethod
    def from_dict(cls, data: dict) -> "Finding | None":
        """Inverse of to_dict(). Returns None for a malformed entry so a
        caller can skip it rather than crash."""
        check_id, file_path = data.get("check_id"), data.get("file")
        if not check_id or not file_path:
            return None
        return cls(
            check_id=check_id,
            tier=data.get("tier") or "rules",
            source=data.get("source") or "unknown",
            severity=Severity.parse(data.get("severity")),
            title=data.get("title") or "",
            explanation=data.get("explanation") or "",
            file=file_path,
            line_start=data.get("line_start") if isinstance(data.get("line_start"), int) else 1,
            line_end=data.get("line_end") if isinstance(data.get("line_end"), int) else None,
            suggestion=data.get("suggestion"),
            raw=data.get("raw"),
        )


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
    # sha256 of each reviewed file's content, for --resume-from.
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

    def compliance_percentage(self) -> float:
        """A single 0-100 score, weighted by finding severity. Each reviewed
        file starts with full credit (_MAX_FILE_WEIGHT) and loses its
        findings' severity weight, floored at zero. Counts findings from
        every source."""
        if not self.files_reviewed:
            return 100.0
        penalty_by_file: dict[str, float] = {}
        for f in self.findings:
            penalty_by_file[f.file] = penalty_by_file.get(f.file, 0.0) + SEVERITY_WEIGHT[f.severity]
        total_score = sum(
            max(0.0, _MAX_FILE_WEIGHT - penalty_by_file.get(path, 0.0))
            for path in self.files_reviewed
        )
        max_possible = len(self.files_reviewed) * _MAX_FILE_WEIGHT
        return round((total_score / max_possible) * 100, 1)

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
            "compliance_percentage": self.compliance_percentage(),
            "findings": [f.to_dict() for f in self.findings],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewReport":
        """Inverse of to_dict(). Tolerant of a missing/malformed generated_at
        (falls back to now)."""
        generated_at_raw = data.get("generated_at")
        generated_at = None
        if isinstance(generated_at_raw, str) and generated_at_raw:
            try:
                generated_at = datetime.fromisoformat(generated_at_raw)
            except ValueError:
                generated_at = None
        raw_findings = data.get("findings")
        findings = (
            [f for f in (Finding.from_dict(d) for d in raw_findings if isinstance(d, dict)) if f]
            if isinstance(raw_findings, list)
            else []
        )
        return cls(
            repo_path=data.get("repo_path") or "",
            mode=data.get("mode") or "diff",
            base_ref=data.get("base_ref"),
            head_ref=data.get("head_ref"),
            generated_at=generated_at or datetime.now(timezone.utc),
            tiers_run=data.get("tiers_run") if isinstance(data.get("tiers_run"), list) else [],
            findings=findings,
            files_reviewed=(
                data.get("files_reviewed") if isinstance(data.get("files_reviewed"), list) else []
            ),
            duration_seconds=data.get("duration_seconds") or 0.0,
            skipped=data.get("skipped") if isinstance(data.get("skipped"), list) else [],
            file_hashes=data.get("file_hashes") if isinstance(data.get("file_hashes"), dict) else {},
        )
