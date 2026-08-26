"""Opt-in second pass (`--suggest-fixes`) that asks an already-configured LLM
tier for a short, targeted fix suggestion on findings that don't already have
one -- most rules-tier findings (ruff, house rules) come with a title and
explanation but no `suggestion` text. This is deliberately narrower than a
full review pass: it answers one specific question ("how would you fix this
exact, already-identified issue") per finding, instead of re-reading the
whole file hunting for new problems -- cheaper, and the answer is scoped
tightly enough that hallucinating a plausible-sounding but wrong fix is less
likely than in an open-ended review.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from codecheck.diff import read_file_content
from codecheck.models import Finding, ReviewTarget
from codecheck.reviewers.openai_protocol import format_http_error, post_with_retry

SYSTEM_PROMPT = (
    "You are a senior code reviewer. You will be given a single, already-identified "
    "issue in a file, plus that file's content. Reply with ONLY a concise, concrete "
    "fix suggestion for that specific issue -- a sentence or two, or a short code "
    "snippet if that's clearer. Do not restate the problem, do not discuss unrelated "
    "code, and do not add a preamble or sign-off."
)


def _build_prompt(finding: Finding, content: str) -> str:
    return (
        f"File: {finding.file}\n"
        f"Issue ({finding.check_id}): {finding.title}\n"
        f"{finding.explanation}\n"
        f"At line {finding.line_start}.\n\n"
        f"File content:\n```\n{content}\n```"
    )


def _eligible_findings(
    findings: list[Finding],
    targets_by_path: dict[str, ReviewTarget],
    exclude_checks: set[str],
    max_suggestions: int,
) -> list[Finding]:
    eligible = [
        f
        for f in findings
        if f.suggestion is None and f.check_id not in exclude_checks and f.file in targets_by_path
    ]
    eligible.sort(key=lambda f: -f.severity.rank)
    return eligible[:max_suggestions]


def generate_suggestions(
    reviewer,
    findings: list[Finding],
    repo_path: Path,
    targets_by_path: dict[str, ReviewTarget],
    max_suggestions: int,
    exclude_checks: set[str],
) -> tuple[int, list[str]]:
    """Fills in `.suggestion` in place on up to `max_suggestions` eligible
    findings (no existing suggestion, not excluded, highest severity first),
    using `reviewer` -- an already-constructed, already-available
    OpenAIProtocolReviewer subclass instance (the cloud or local tier), reused
    here purely for its client/auth/base_url/model, not its own review()
    method. Returns (count actually filled in, skip-reason strings) -- a
    per-finding failure here is recorded and moved past, never raised, since
    a fix-suggestion pass failing shouldn't take down a run that already has
    real findings to report.
    """
    targets = _eligible_findings(findings, targets_by_path, exclude_checks, max_suggestions)
    if not targets:
        return 0, []

    client = reviewer._get_client()
    base_url = reviewer._resolved_base_url()
    skipped: list[str] = []
    count = 0
    for finding in targets:
        target = targets_by_path[finding.file]
        content = read_file_content(repo_path, target)
        if content is None:
            skipped.append(f"suggest_fixes: {finding.file}: could not read file content")
            continue
        payload = {
            "model": reviewer.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_prompt(finding, content)},
            ],
            "max_tokens": 400,
        }
        try:
            response = post_with_retry(client, base_url, payload)
        except httpx.HTTPError as e:
            skipped.append(
                f"suggest_fixes: {finding.file}:{finding.line_start}: {format_http_error(e)}"
            )
            continue
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            skipped.append(
                f"suggest_fixes: {finding.file}:{finding.line_start}: unexpected response shape"
            )
            continue
        if not isinstance(text, str) or not text.strip():
            skipped.append(f"suggest_fixes: {finding.file}:{finding.line_start}: empty response")
            continue
        finding.suggestion = text.strip()
        count += 1
    return count, skipped
