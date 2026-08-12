"""Merges findings from whichever tiers ran, dedupes near-identical findings
(same file + overlapping line range + similar title), and assigns a combined
severity. No embeddings — cheap difflib similarity is enough for this.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from codecheck.models import Finding

_TIER_PRIORITY = {"cloud_llm": 3, "local_llm": 2, "rules": 1}
_LINE_WINDOW = 2
_TITLE_SIMILARITY_THRESHOLD = 0.6


def _ranges_overlap(a: Finding, b: Finding) -> bool:
    a_start, a_end = a.line_start, a.line_end or a.line_start
    b_start, b_end = b.line_start, b.line_end or b.line_start
    return a_start - _LINE_WINDOW <= b_end and b_start - _LINE_WINDOW <= a_end


def _titles_similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= _TITLE_SIMILARITY_THRESHOLD


def _is_duplicate(a: Finding, b: Finding) -> bool:
    return a.file == b.file and _ranges_overlap(a, b) and _titles_similar(a.title, b.title)


def _tier_priority(finding: Finding) -> int:
    return _TIER_PRIORITY.get(finding.tier, 0)


def _merge_pair(primary: Finding, secondary: Finding) -> Finding:
    severity = primary.severity if primary.severity >= secondary.severity else secondary.severity
    raw = dict(primary.raw or {})
    also_flagged_by = raw.get("also_flagged_by", [])
    also_flagged_by = [*also_flagged_by, f"{secondary.source}:{secondary.check_id}"]
    raw["also_flagged_by"] = also_flagged_by
    return Finding(
        check_id=primary.check_id,
        tier=primary.tier,
        source=primary.source,
        severity=severity,
        title=primary.title,
        explanation=primary.explanation,
        file=primary.file,
        line_start=primary.line_start,
        line_end=primary.line_end,
        suggestion=primary.suggestion,
        raw=raw,
    )


def aggregate(results: dict[str, list[Finding]]) -> list[Finding]:
    all_findings: list[Finding] = []
    for tier_findings in results.values():
        all_findings.extend(tier_findings)

    merged: list[Finding] = []
    for finding in all_findings:
        duplicate_index = None
        for i, existing in enumerate(merged):
            if _is_duplicate(finding, existing):
                duplicate_index = i
                break

        if duplicate_index is None:
            merged.append(finding)
            continue

        existing = merged[duplicate_index]
        if _tier_priority(finding) > _tier_priority(existing):
            merged[duplicate_index] = _merge_pair(finding, existing)
        else:
            merged[duplicate_index] = _merge_pair(existing, finding)

    merged.sort(key=lambda f: (-f.severity.rank, f.file, f.line_start))
    return merged
