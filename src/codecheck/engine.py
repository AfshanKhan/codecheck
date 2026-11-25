"""Core review engine: in-process orchestration of the multi-tier review pipeline.

Encapsulates target preflight, tier execution, resume caching, findings aggregation,
fix suggestions, and report compilation into a single, cohesive module callable from
CLI, Web UI, or programmatic API.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from codecheck.aggregator import aggregate
from codecheck.config import Config
from codecheck.diff import read_file_content
from codecheck.frappe_db import FrappeDbConnection
from codecheck.models import Finding, ReviewReport, ReviewTarget, Severity
from codecheck.noise import partition, summary
from codecheck.resume import (
    already_succeeded_paths,
    compute_file_hash,
    load_prior_report,
    prior_findings_for_paths,
)
from codecheck.reviewers.cloud_llm import (
    build_cloud_reviewer,
    cloud_targets,
    exceeds_audit_cap,
    without_ci_files,
)
from codecheck.reviewers.local_llm import LocalLLMReviewer
from codecheck.reviewers.openai_protocol import OpenAIProtocolReviewer
from codecheck.reviewers.rules_engine import RulesEngineReviewer
from codecheck.source import AcquiredWorkspace
from codecheck.suggest import generate_suggestions


class AuditCapExceededError(Exception):
    """Raised when cloud review target count exceeds config.cloud.audit_file_cap."""

    def __init__(self, count: int, cap: int) -> None:
        self.count = count
        self.file_count = count
        self.cap = cap
        super().__init__(f"Refusing to run the cloud tier over {count} files (cap is {cap})")


@dataclass
class ReviewRequest:
    """Inputs and configuration for executing a review."""

    workspace: AcquiredWorkspace
    config: Config
    resume_from: Path | None = None
    suggest_fixes: bool = False
    force_local: bool = False
    force_cloud: bool = False
    device: str | None = None
    ruff_extra_config: list[str] | None = None
    frappe_db: FrappeDbConnection | None = None
    skip_notes: list[str] = field(default_factory=list)

    # Observer callbacks
    on_tier_start: Callable[[str, str], None] | None = None  # (tier, message)
    on_file_progress: Callable[[str, str, str], None] | None = None  # (tier, file, outcome)
    on_resumed: Callable[[str, int], None] | None = None  # (tier, count)
    on_suggestions_generated: Callable[[int], None] | None = None  # (count)
    confirm_local_execution: Callable[[Config, bool, str | None, list[str]], bool] | None = None


@dataclass
class ReviewExecutionResult:
    """The result of executing a review pipeline."""

    report: ReviewReport
    had_runner_error: bool
    gate_passed: bool
    fail_threshold: Severity
    skipped_messages: list[str]
    llm_sent: dict[str, list[str]]


def compute_current_file_hashes(targets: list[ReviewTarget], repo_path: Path) -> dict[str, str]:
    """sha256 of each target's current content, keyed by path, for --resume-from."""
    hashes = {}
    for target in targets:
        content = read_file_content(repo_path, target)
        if content is not None:
            hashes[target.path] = compute_file_hash(content)
    return hashes


def llm_tier_failed_completely(reviewer, resumed: int) -> bool:
    """True when an LLM tier tried to review files and every attempt failed."""
    return bool(reviewer.failed_files) and reviewer.reviewed_count == 0 and resumed == 0


def run_llm_tier(
    tier: str,
    reviewer,
    targets: list[ReviewTarget],
    repo_path: Path,
    prior_report: dict | None,
    mode: str,
    current_file_hashes: dict[str, str],
    on_progress: Callable[[str, str], None] | None = None,
) -> tuple[list[Finding], list[str], int]:
    """Runs one LLM tier's review(), reusing prior_report results for targets it already succeeded on."""
    already_done: set[str] = set()
    if prior_report is not None:
        target_paths = {t.path for t in targets}
        already_done = (
            already_succeeded_paths(prior_report, tier, mode, current_file_hashes) & target_paths
        )

    remaining_targets = [t for t in targets if t.path not in already_done]
    sent_paths = sorted({t.path for t in remaining_targets} | already_done)
    left_out_note = None

    if getattr(getattr(reviewer, "config", None), "skip_noise", False):
        remaining_targets, left_out = partition(remaining_targets, repo_path)
        left_out_note = summary(left_out) if left_out else None
        sent_paths = sorted({t.path for t in remaining_targets} | already_done)

    reviewer.sent_paths = sent_paths

    new_findings = reviewer.review(remaining_targets, repo_path, on_progress=on_progress)

    reused_findings = (
        prior_findings_for_paths(prior_report, tier, already_done)
        if (prior_report is not None and already_done)
        else []
    )
    skip_entries = [f"{tier}: {path}: {reason}" for path, reason in reviewer.skipped_files]
    if left_out_note:
        skip_entries.insert(0, f"{tier}: {left_out_note}")

    return reused_findings + new_findings, skip_entries, len(already_done)


class ReviewEngine:
    """Executes the multi-tier review pipeline in-process."""

    def review(self, request: ReviewRequest) -> ReviewExecutionResult:
        start_time = time.monotonic()
        ws = request.workspace
        cfg = request.config
        targets = ws.targets
        repo_path = ws.repo_path

        if not targets:
            report = ReviewReport(
                repo_path=str(ws.repo_path),
                mode=ws.mode,
                base_ref=ws.base_ref,
                head_ref=ws.head_ref,
                generated_at=datetime.now(timezone.utc),
                tiers_run=[],
                findings=[],
                files_reviewed=[],
                duration_seconds=round(time.monotonic() - start_time, 2),
                skipped=list(request.skip_notes),
            )
            return ReviewExecutionResult(
                report=report,
                had_runner_error=False,
                gate_passed=True,
                fail_threshold=Severity(cfg.thresholds.fail_on_severity),
                skipped_messages=list(request.skip_notes),
                llm_sent={},
            )

        if cfg.cloud.enabled:
            capped_count = exceeds_audit_cap(
                targets, cfg.cloud, force=request.force_cloud, repo_path=repo_path
            )
            if capped_count is not None:
                raise AuditCapExceededError(capped_count, cfg.cloud.audit_file_cap)

        llm_sent: dict[str, list[str]] = {}
        results, tiers_run, skipped, _hashes, had_runner_error = self._run_tiers(
            request, llm_sent
        )

        all_skipped = list(request.skip_notes) + skipped
        findings = aggregate(results)

        if request.suggest_fixes:
            self._maybe_suggest_fixes(request, findings, all_skipped)

        report = ReviewReport(
            repo_path=str(ws.repo_path),
            mode=ws.mode,
            base_ref=ws.base_ref,
            head_ref=ws.head_ref,
            generated_at=datetime.now(timezone.utc),
            tiers_run=tiers_run,
            findings=findings,
            files_reviewed=[t.path for t in targets],
            duration_seconds=round(time.monotonic() - start_time, 2),
            skipped=all_skipped,
            file_hashes=_hashes,
            llm_sent=llm_sent,
        )

        fail_threshold = Severity(cfg.thresholds.fail_on_severity)
        gate_passed = not report.findings_at_or_above(fail_threshold)

        return ReviewExecutionResult(
            report=report,
            had_runner_error=had_runner_error,
            gate_passed=gate_passed,
            fail_threshold=fail_threshold,
            skipped_messages=all_skipped,
            llm_sent=llm_sent,
        )

    def _run_tiers(
        self, request: ReviewRequest, sent_out: dict[str, list[str]]
    ) -> tuple[dict[str, list[Finding]], list[str], list[str], dict[str, str], bool]:
        ws = request.workspace
        cfg = request.config
        targets = ws.targets
        repo_path = ws.repo_path

        results: dict[str, list[Finding]] = {}
        tiers_run: list[str] = []
        skipped: list[str] = []
        had_runner_error = False

        prior_report: dict | None = None
        if request.resume_from is not None:
            prior_report = load_prior_report(request.resume_from)
            if prior_report is None:
                skipped.append(f"--resume-from: could not read or parse {request.resume_from}")

        current_file_hashes = compute_current_file_hashes(targets, repo_path)

        # 1. Rules Engine Tier
        rules_reviewer = RulesEngineReviewer(
            cfg.rules, frappe_db=request.frappe_db, ruff_extra_config=request.ruff_extra_config
        )
        available, reason = rules_reviewer.is_available(repo_path)
        if available:
            if request.on_tier_start:
                request.on_tier_start("rules", "Running rules engine (ruff/eslint/semgrep/house rules)...")
            results["rules"] = rules_reviewer.review(targets, repo_path)
            tiers_run.append("rules")
            skipped.extend(f"rules: {name}: {r}" for name, r in rules_reviewer.skipped_runners)
            had_runner_error = bool(rules_reviewer.runner_failures)
        elif reason:
            skipped.append(f"rules tier: {reason}")

        # 2. Local LLM Tier
        local_reviewer = LocalLLMReviewer(cfg.local)
        available, reason = local_reviewer.is_available(repo_path)
        confirmed = True
        if available and request.confirm_local_execution:
            confirmed = request.confirm_local_execution(
                cfg, request.force_local, request.device, skipped
            )

        if available and confirmed:
            if request.on_tier_start:
                request.on_tier_start("local_llm", "Running local LLM review...")

            def _on_local_progress(path: str, outcome: str):
                if request.on_file_progress:
                    request.on_file_progress("local_llm", path, outcome)

            findings, skip_entries, resumed = run_llm_tier(
                "local_llm",
                local_reviewer,
                cloud_targets(targets, cfg.local),
                repo_path,
                prior_report,
                ws.mode,
                current_file_hashes,
                on_progress=_on_local_progress,
            )
            results["local_llm"] = findings
            sent_out["local_llm"] = local_reviewer.sent_paths
            skipped.extend(skip_entries)

            if llm_tier_failed_completely(local_reviewer, resumed):
                had_runner_error = True
            else:
                tiers_run.append("local_llm")

            if resumed and request.on_resumed:
                request.on_resumed("local_llm", resumed)
        elif cfg.local.enabled and reason:
            skipped.append(f"local tier: {reason}")

        # 3. Cloud LLM Tier
        cloud_reviewer = build_cloud_reviewer(cfg.cloud)
        available, reason = cloud_reviewer.is_available(repo_path)
        if available:
            if request.on_tier_start:
                request.on_tier_start("cloud_llm", "Running cloud LLM review...")

            def _on_cloud_progress(path: str, outcome: str):
                if request.on_file_progress:
                    request.on_file_progress("cloud_llm", path, outcome)

            findings, skip_entries, resumed = run_llm_tier(
                "cloud_llm",
                cloud_reviewer,
                cloud_targets(targets, cfg.cloud),
                repo_path,
                prior_report,
                ws.mode,
                current_file_hashes,
                on_progress=_on_cloud_progress,
            )
            results["cloud_llm"] = findings
            sent_out["cloud_llm"] = getattr(cloud_reviewer, "sent_paths", [])
            skipped.extend(skip_entries)

            if llm_tier_failed_completely(cloud_reviewer, resumed):
                had_runner_error = True
            else:
                tiers_run.append("cloud_llm")

            if resumed and request.on_resumed:
                request.on_resumed("cloud_llm", resumed)
        elif cfg.cloud.enabled and reason:
            skipped.append(f"cloud tier: {reason}")

        return (
            results,
            tiers_run,
            skipped,
            current_file_hashes,
            had_runner_error or not tiers_run,
        )

    def _maybe_suggest_fixes(
        self, request: ReviewRequest, findings: list[Finding], skipped: list[str]
    ) -> None:
        cfg = request.config
        repo_path = request.workspace.repo_path
        targets = request.workspace.targets

        reviewer = None
        cloud_unsupported_reason: str | None = None
        if cfg.cloud.enabled:
            candidate = build_cloud_reviewer(cfg.cloud)
            if isinstance(candidate, OpenAIProtocolReviewer):
                available, reason = candidate.is_available(repo_path)
                if available:
                    reviewer = candidate
                else:
                    cloud_unsupported_reason = reason or "the configured cloud provider isn't available"
            else:
                cloud_unsupported_reason = (
                    f"cloud.provider={cfg.cloud.provider!r} isn't supported for suggestions yet "
                    "(Anthropic has no suggestion-compatible client -- use a non-Anthropic, "
                    "OpenAI-compatible cloud provider instead)"
                )

        if reviewer is None and cfg.local.enabled:
            candidate = LocalLLMReviewer(cfg.local)
            available, _reason = candidate.is_available(repo_path)
            if available:
                reviewer = candidate
                if cloud_unsupported_reason is not None:
                    skipped.append(
                        f"suggest_fixes: cloud unusable ({cloud_unsupported_reason}) -- "
                        "falling back to --local for suggestions instead"
                    )

        if reviewer is None:
            skipped.append(
                "suggest_fixes: requires --cloud (a non-Anthropic, OpenAI-compatible provider -- "
                "Anthropic isn't supported for suggestions yet) or --local with a usable, "
                "available provider to generate suggestions"
            )
            return

        targets_by_path = {t.path: t for t in without_ci_files(targets)}
        exclude_checks = set(cfg.suggestions.exclude_checks)

        count, suggest_skips = generate_suggestions(
            reviewer, findings, repo_path, targets_by_path, cfg.suggestions.max_per_run, exclude_checks
        )
        skipped.extend(suggest_skips)
        if count and request.on_suggestions_generated:
            request.on_suggestions_generated(count)
