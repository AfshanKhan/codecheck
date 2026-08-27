"""Resume support for the cloud/local LLM tiers: `--resume-from <prior
report.json>` lets a re-run skip files a tier already got a real answer
for, carrying that answer forward into the new report. Only applies to
cloud_llm/local_llm; the rules tier just re-runs in full every time."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from codecheck.models import Finding, Severity


def load_prior_report(path: Path) -> dict | None:
    """Returns the parsed prior report.json, or None if it can't be read/parsed."""
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def compute_file_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


def already_succeeded_paths(
    prior: dict, tier: str, current_mode: str, current_file_hashes: dict[str, str]
) -> set[str]:
    """Files `tier` already produced a real (non-skipped) result for in the
    prior run -- safe to skip re-requesting. Requires both the prior
    report's mode to match the current run's, and the file's content hash
    to be identical -- not just the path (repo_path isn't checked, since a
    --repo-url run clones to a fresh temp directory every invocation)."""
    tiers_run = prior.get("tiers_run")
    if not isinstance(tiers_run, list) or tier not in tiers_run:
        return set()
    if prior.get("mode") != current_mode:
        return set()
    prior_files = prior.get("files_reviewed")
    prior_skipped = prior.get("skipped")
    prior_hashes = prior.get("file_hashes")
    if not isinstance(prior_files, list) or not isinstance(prior_skipped, list):
        return set()
    if not isinstance(prior_hashes, dict):
        return set()  # older report predating file_hashes -- fail closed, not stale-trusting

    result = set()
    for path in prior_files:
        if not isinstance(path, str):
            continue
        prior_hash = prior_hashes.get(path)
        current_hash = current_file_hashes.get(path)
        if not prior_hash or not current_hash or prior_hash != current_hash:
            continue  # content changed (or unknown) -- must be re-reviewed
        if any(
            isinstance(entry, str) and entry.startswith(f"{tier}: {path}: ") for entry in prior_skipped
        ):
            continue
        result.add(path)
    return result


def prior_findings_for_paths(prior: dict, tier: str, paths: set[str]) -> list[Finding]:
    """Reconstructs Finding objects from the prior report's JSON for the
    given (already-succeeded) paths."""
    findings = []
    for raw in prior.get("findings", []):
        if not isinstance(raw, dict):
            continue
        if raw.get("tier") != tier or raw.get("file") not in paths:
            continue
        check_id, file_path = raw.get("check_id"), raw.get("file")
        if not check_id or not file_path:
            continue  # missing a required field -- skip rather than guess
        findings.append(
            Finding(
                check_id=check_id,
                tier=tier,
                source=raw.get("source", tier),
                severity=Severity.parse(raw.get("severity")),
                title=raw.get("title", ""),
                explanation=raw.get("explanation", ""),
                file=file_path,
                line_start=raw.get("line_start") if isinstance(raw.get("line_start"), int) else 1,
                line_end=raw.get("line_end") if isinstance(raw.get("line_end"), int) else None,
                suggestion=raw.get("suggestion"),
                raw=raw.get("raw"),
            )
        )
    return findings
