"""Resume support for the cloud/local LLM tiers: `--resume-from <prior report.json>`
lets a re-run skip files a tier already got a real answer for, and carries that
prior answer forward into the new report -- instead of re-requesting every file
from scratch and re-burning the same rate-limit budget on the same files every
time (confirmed: a free-tier provider that can't cover a whole repo in one run
always fails on the same files first, since targets are processed in a fixed
order -- blind retries never reach the back of the list).

Only applies to cloud_llm/local_llm; the rules tier is free and fast enough to
just re-run in full every time.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from codecheck.models import Finding, Severity


def load_prior_report(path: Path) -> dict | None:
    """Returns the parsed prior report.json, or None if it can't be read/parsed
    -- callers should treat None as "no resume data available," not an error,
    since a resumed run should still work as a plain fresh run in that case.
    """
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
    prior run -- safe to skip re-requesting these.

    Path alone isn't enough to trust a prior result: the prior report could
    be from a different review mode (a diff-mode review only ever saw the
    changed-lines context, an audit-mode review saw the whole file -- these
    aren't the same question even for an identical file) or the file's
    content could simply have changed since that report was generated (the
    working tree was edited, a different commit/PR is now in scope, etc.).
    Confirmed as a real risk via Greptile review: reusing findings in either
    case would either mismatch what was actually asked, or silently mask a
    real change in the file with a stale result. So this requires *both*:
    the prior report's mode matches the current run's mode, and the file's
    content hash is identical between the two runs -- not just the path.

    (repo_path is deliberately NOT checked here: a --repo-url run clones to
    a fresh temp directory every single invocation, so requiring repo_path
    equality would make --resume-from useless for exactly the case it exists
    for. Byte-identical content at the same path, in the same review mode,
    is what actually makes a prior result valid to reuse -- not which
    directory it happened to sit in.)
    """
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
    """Reconstructs Finding objects from the prior report's JSON for the given
    (already-succeeded) paths, so a resumed run's report is the union of both
    runs' results, not just the new run's delta.
    """
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
