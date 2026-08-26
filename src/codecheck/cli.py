from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from codecheck.aggregator import aggregate
from codecheck.config import Config, load_config
from codecheck.diff import get_diff, read_file_content
from codecheck.frappe_db import FrappeDbConnection, FrappeDbUnavailable
from codecheck.github_source import cloned_repo, parse_pr_url, pr_worktree
from codecheck.lm_link import resolve_model_location, set_preferred_device
from codecheck.models import ReviewReport, ReviewTarget, Severity
from codecheck.redact import redact_report
from codecheck.repo_scan import get_repo_files
from codecheck.report_diff import diff_reports
from codecheck.reporters.console import print_report
from codecheck.reporters.docx_report import write_docx_report
from codecheck.reporters.json_report import write_json_report
from codecheck.reporters.markdown_report import write_markdown_report
from codecheck.resume import (
    already_succeeded_paths,
    compute_file_hash,
    load_prior_report,
    prior_findings_for_paths,
)
from codecheck.reviewers.cloud_llm import build_cloud_reviewer, exceeds_audit_cap
from codecheck.reviewers.local_llm import LocalLLMReviewer
from codecheck.reviewers.openai_protocol import OpenAIProtocolReviewer
from codecheck.reviewers.rules_engine import RulesEngineReviewer
from codecheck.suggest import generate_suggestions

app = typer.Typer(add_completion=False)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        try:
            resolved = package_version("codecheck")
        except PackageNotFoundError:  # not installed as a distribution (e.g. run from source)
            resolved = "unknown"
        console.print(f"codecheck {resolved}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the installed codecheck version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
):
    """codecheck: local code review tool — review a diff or audit a whole repo,
    with deterministic rules and an optional cloud LLM pass."""


def _match_candidate(candidates: list, device: str):
    device_lower = device.strip().lower()
    if device_lower in ("local", "this-machine", "this machine"):
        return next((c for c in candidates if "local" in c.label.lower()), None)
    for c in candidates:
        if c.label.lower() == device_lower or device_lower in c.label.lower():
            return c
    return None


def _resolve_ambiguous_device(location, device: str | None, force_local: bool, skipped: list[str]):
    """The same model can be loaded on more than one device at once (we
    confirmed this against a real LM Link setup, and that LM Studio silently
    picks one without telling the caller which). Returns the chosen candidate,
    or None if none was chosen (caller should skip the local tier).
    """
    if device:
        chosen = _match_candidate(location.candidates, device)
        if chosen is None:
            names = ", ".join(c.label for c in location.candidates)
            console.print(f"[red]--device {device!r} does not match any candidate ({names}).[/red]")
            skipped.append(f"local tier: --device {device!r} did not match any loaded device")
        return chosen

    if sys.stdin.isatty():
        console.print("[yellow]This model is loaded on multiple devices — choose which one to use:[/yellow]")
        for i, c in enumerate(location.candidates, start=1):
            console.print(f"  {i}. {c.label}")
        choice = typer.prompt("Enter a number", default="0")
        try:
            index = int(choice)
        except ValueError:
            index = 0
        if 1 <= index <= len(location.candidates):
            return location.candidates[index - 1]
        skipped.append("local tier: no valid device selected")
        return None

    if force_local:
        skipped.append(
            "local tier: ambiguous device, proceeding with --force-local without setting a preference "
            "(LM Studio will pick one on its own)"
        )
        return "unset"  # sentinel: proceed without calling set_preferred_device

    skipped.append(
        "local tier: model is loaded on multiple devices and the choice is ambiguous — "
        "pass --device <name> to pick one, or --force-local to accept LM Studio's default"
    )
    return None


def _confirm_local_execution(
    cfg: Config, force_local: bool, device: str | None, skipped: list[str]
) -> bool:
    """Before running the local tier, check which device will actually serve
    cfg.local.model (this machine, a remote LM Link device, or ambiguously
    both) and require confirmation/a choice if it isn't a confirmed single
    remote device. Returns whether to proceed.

    LM Link is an LM Studio-specific feature -- Ollama and generic
    openai_compatible servers have no equivalent multi-device concept, so this
    whole gate is skipped for them rather than forcing a confirmation prompt
    that describes a scenario that can't actually happen.
    """
    if cfg.local.provider != "lm_studio":
        return True

    if not cfg.local.model:
        return True  # LocalLLMReviewer.is_available() will already reject this with a clearer message

    location = resolve_model_location(cfg.local.model)
    console.print(f"[dim]Local LLM tier: {location.description}[/dim]")

    if location.is_ambiguous:
        chosen = _resolve_ambiguous_device(location, device, force_local, skipped)
        if chosen is None:
            return False
        if chosen == "unset":
            return True
        ok, msg = set_preferred_device(chosen.device_identifier)
        if not ok:
            console.print(f"[red]Could not set preferred device: {msg}[/red]")
            skipped.append(f"local tier: could not set preferred device to {chosen.label!r}: {msg}")
            return False
        console.print(
            f"[dim]Set LM Studio's preferred device to {chosen.label!r} "
            f"(persists as an LM Studio setting beyond this run).[/dim]"
        )
        return True

    if location.is_local is False:
        return True  # confirmed running on a single remote LM Link device

    if force_local:
        return True

    if sys.stdin.isatty():
        proceed = typer.confirm(
            "This will use THIS machine's compute (not a confirmed remote LM Link device). Proceed?",
            default=False,
        )
        if not proceed:
            skipped.append("local tier: declined by user (would run on this machine, not confirmed remote)")
        return proceed

    skipped.append(
        "local tier: would run on this machine (or the device couldn't be confirmed) — "
        "pass --force-local to proceed non-interactively"
    )
    return False


def _compute_current_file_hashes(targets: list[ReviewTarget], repo_path: Path) -> dict[str, str]:
    """sha256 of each target's current content, keyed by path -- the signal
    --resume-from uses to confirm a file hasn't changed since a prior run
    before trusting that prior run's result for it. Deleted files (no
    content) are simply absent, same as reviewers already treat them.
    """
    hashes = {}
    for target in targets:
        content = read_file_content(repo_path, target)
        if content is not None:
            hashes[target.path] = compute_file_hash(content)
    return hashes


def _run_llm_tier(
    tier: str,
    reviewer,
    targets: list[ReviewTarget],
    repo_path: Path,
    prior_report: dict | None,
    mode: str,
    current_file_hashes: dict[str, str],
    status_message: str,
) -> tuple[list, list[str], int]:
    """Runs one LLM tier's review(), skipping any target this tier already
    succeeded on in prior_report (--resume-from) and merging that prior run's
    findings back in for those files. Returns (findings, skip_entries,
    resumed_count).
    """
    already_done: set[str] = set()
    if prior_report is not None:
        target_paths = {t.path for t in targets}
        already_done = (
            already_succeeded_paths(prior_report, tier, mode, current_file_hashes) & target_paths
        )

    remaining_targets = [t for t in targets if t.path not in already_done]
    total = len(remaining_targets)
    completed = 0

    def _on_progress(path: str, outcome: str) -> None:
        nonlocal completed
        completed += 1
        console.print(f"[dim]  ({completed}/{total}) {path}: {outcome}[/dim]")

    with console.status(f"[bold]{status_message}"):
        new_findings = reviewer.review(remaining_targets, repo_path, on_progress=_on_progress)

    reused_findings = (
        prior_findings_for_paths(prior_report, tier, already_done) if already_done else []
    )
    skip_entries = [f"{tier}: {path}: {reason}" for path, reason in reviewer.skipped_files]
    return reused_findings + new_findings, skip_entries, len(already_done)


def _run_tiers(
    targets: list[ReviewTarget],
    repo_path: Path,
    cfg: Config,
    mode: str,
    force_local: bool = False,
    device: str | None = None,
    resume_from: Path | None = None,
    frappe_db: FrappeDbConnection | None = None,
) -> tuple[dict[str, list], list[str], list[str], dict[str, str]]:
    """Runs the rules tier and (if available) the local and cloud LLM tiers over
    the given targets. Returns (results_by_tier, tiers_run, skip_reasons,
    current_file_hashes) -- the last is stashed on the produced ReviewReport
    so a *later* run can validate against it via --resume-from.

    resume_from, if given, points at a prior run's report.json: any file an
    LLM tier already got a real (non-skipped) result for there is skipped
    this run too, and that prior result is carried into the new report --
    but only if the prior run was the same mode (diff vs audit) and the
    file's content hasn't changed since; see already_succeeded_paths for why
    path alone isn't a safe enough signal. Only files that were skipped last
    time (rate limit, transient error, ...) get re-requested. The rules tier
    always re-runs in full regardless, since it's free and fast.
    """
    results: dict[str, list] = {}
    tiers_run: list[str] = []
    skipped: list[str] = []

    prior_report: dict | None = None
    if resume_from is not None:
        prior_report = load_prior_report(resume_from)
        if prior_report is None:
            skipped.append(f"--resume-from: could not read or parse {resume_from}")

    current_file_hashes = _compute_current_file_hashes(targets, repo_path)

    rules_reviewer = RulesEngineReviewer(cfg.rules, frappe_db=frappe_db)
    available, reason = rules_reviewer.is_available(repo_path)
    if available:
        with console.status("[bold]Running rules engine (ruff/eslint/semgrep/house rules)..."):
            results["rules"] = rules_reviewer.review(targets, repo_path)
        tiers_run.append("rules")
        skipped.extend(f"rules: {name}: {reason}" for name, reason in rules_reviewer.skipped_runners)
    elif reason:
        skipped.append(f"rules tier: {reason}")

    local_reviewer = LocalLLMReviewer(cfg.local)
    available, reason = local_reviewer.is_available(repo_path)
    if available and _confirm_local_execution(cfg, force_local, device, skipped):
        findings, skip_entries, resumed = _run_llm_tier(
            "local_llm", local_reviewer, targets, repo_path, prior_report, mode,
            current_file_hashes, "Running local LLM review...",
        )
        results["local_llm"] = findings
        tiers_run.append("local_llm")
        skipped.extend(skip_entries)
        if resumed:
            console.print(f"[dim]Resumed: {resumed} file(s) already reviewed by local_llm, not re-requested.[/dim]")
    elif cfg.local.enabled and reason:
        skipped.append(f"local tier: {reason}")

    cloud_reviewer = build_cloud_reviewer(cfg.cloud)
    available, reason = cloud_reviewer.is_available(repo_path)
    if available:
        findings, skip_entries, resumed = _run_llm_tier(
            "cloud_llm", cloud_reviewer, targets, repo_path, prior_report, mode,
            current_file_hashes, "Running cloud LLM review...",
        )
        results["cloud_llm"] = findings
        tiers_run.append("cloud_llm")
        skipped.extend(skip_entries)
        if resumed:
            console.print(f"[dim]Resumed: {resumed} file(s) already reviewed by cloud_llm, not re-requested.[/dim]")
    elif cfg.cloud.enabled and reason:
        skipped.append(f"cloud tier: {reason}")

    return results, tiers_run, skipped, current_file_hashes


def _sanitize_slug(value: str) -> str:
    return "".join(c for c in value if c.isalnum() or c in ("-", "_"))


def _repo_label(repo_path: Path, repo_url: str | None) -> str:
    """A short, filesystem-safe name for the reviewed repo, used in report
    filenames. Prefers owner_repo parsed from --repo-url/--pr's URL over the
    local directory name -- cloned repos land in a randomly-named temp dir
    (see github_source.cloned_repo), so the directory name itself is useless
    there.
    """
    if repo_url:
        cleaned = repo_url.strip().rstrip("/")
        if cleaned.endswith(".git"):
            cleaned = cleaned[: -len(".git")]
        parts = [p for p in re.split(r"[/:]", cleaned) if p]
        if len(parts) >= 2:
            slug = f"{_sanitize_slug(parts[-2])}_{_sanitize_slug(parts[-1])}"
        elif parts:
            slug = _sanitize_slug(parts[-1])
        else:
            slug = ""
    else:
        slug = _sanitize_slug(repo_path.resolve().name)
    return slug or "repo"


def _report_basename(repo_label: str, pr_number: int | None, mode: str, generated_at: datetime) -> str:
    suffix = f"pr{pr_number}" if pr_number is not None else mode
    timestamp = generated_at.strftime("%Y%m%d_%H%M%S")
    return f"{repo_label}_{suffix}_{timestamp}"


_REPORT_EXTENSIONS = (".json", ".md", ".docx")


def _claim_unique_basename(output_dir: Path, basename: str) -> str:
    """The timestamp in `basename` only has second resolution, so two runs for
    the same repo/PR/mode finishing within the same second would otherwise
    collide and silently overwrite each other's reports. Reserves all three
    `<candidate>.{json,md,docx}` paths with an atomic exclusive-create
    (O_CREAT | O_EXCL) each, not a check-then-write -- an exists() check
    followed by a later write has a TOCTOU gap two concurrent codecheck
    processes could both pass, still overwriting the same paths. Claiming
    only the `.json` path isn't enough either: a partial leftover (e.g. an
    interrupted prior run, or the `.json` deleted by hand) could leave `.md`/
    `.docx` behind with no matching `.json`, and a claim scoped to `.json`
    alone would then silently overwrite them. If any one of the three is
    already taken, whatever this candidate did manage to claim is rolled back
    before moving on to the next suffix, so a candidate is only ever
    considered "won" once all three are actually free. A non-collision I/O
    error (permission denied, disk full, ...) on the 2nd/3rd extension gets
    the same rollback treatment, then re-raises rather than silently trying
    the next suffix -- an error like that will likely fail identically for
    every candidate, so swallowing it and looping would just leave a trail of
    empty, permanently-claimed files behind for no benefit.
    """
    candidate = basename
    suffix = 2
    while True:
        claimed: list[Path] = []
        try:
            for ext in _REPORT_EXTENSIONS:
                path = output_dir / f"{candidate}{ext}"
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                claimed.append(path)
            return candidate
        except FileExistsError:
            for path in claimed:
                path.unlink(missing_ok=True)
            candidate = f"{basename}-{suffix}"
            suffix += 1
        except OSError:
            for path in claimed:
                path.unlink(missing_ok=True)
            raise


def _write_reports(
    report: ReviewReport, output_dir: Path, repo_label: str, pr_number: int | None = None
) -> tuple[Path, Path, Path]:
    """Claims a unique basename and writes the .json/.md/.docx trio for
    `report`. Shared by `_finish` (a live diff/audit run) and the `render`
    command (re-rendering a prior run's .json with no checks re-run), so both
    get the exact same collision-safe, all-or-nothing write behavior.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    basename = _claim_unique_basename(
        output_dir, _report_basename(repo_label, pr_number, report.mode, report.generated_at)
    )
    json_path = output_dir / f"{basename}.json"
    md_path = output_dir / f"{basename}.md"
    docx_path = output_dir / f"{basename}.docx"
    try:
        write_json_report(report, json_path)
        write_markdown_report(report, md_path)
        write_docx_report(report, docx_path)
    except BaseException:
        # A reporter raising here would otherwise leave this basename
        # permanently claimed by empty/partial files -- no future run could
        # ever reuse it (the atomic claim in _claim_unique_basename sees them
        # as "already exists" forever), and anyone opening one would find an
        # empty or truncated report. Free the name back up instead: delete
        # whatever got claimed/written for this basename and let the
        # original error propagate.
        for path in (json_path, md_path, docx_path):
            path.unlink(missing_ok=True)
        raise
    return json_path, md_path, docx_path


def _finish(
    report: ReviewReport,
    output_dir: Path,
    cfg: Config,
    repo_label: str,
    pr_number: int | None = None,
    redact: bool = False,
) -> None:
    console.print()
    print_report(report, console)

    report_to_write = redact_report(report) if redact else report
    json_path, md_path, docx_path = _write_reports(report_to_write, output_dir, repo_label, pr_number)
    console.print(f"\n[dim]Reports written to {json_path}, {md_path}, and {docx_path}[/dim]")

    fail_threshold = Severity(cfg.thresholds.fail_on_severity)
    if report.findings_at_or_above(fail_threshold):
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


# Named shorthands for thresholds.fail_on_severity, so a CI pipeline (or a
# human) can dial the exit-code gate up or down without spelling out a raw
# severity value or hand-editing config.yaml -- e.g. --gate strict on a
# security-sensitive repo, --gate relaxed while a legacy codebase is still
# working down a large backlog of MEDIUM-severity findings.
_GATE_PROFILES = {"strict": "medium", "standard": "high", "relaxed": "critical"}


def _resolve_gate(gate: str | None) -> str | None:
    if gate is None:
        return None
    resolved = _GATE_PROFILES.get(gate)
    if resolved is None:
        console.print(
            f"[red]Error:[/red] --gate must be one of {', '.join(_GATE_PROFILES)}, got {gate!r}."
        )
        raise typer.Exit(code=2)
    return resolved


def _maybe_suggest_fixes(
    suggest_fixes: bool,
    cfg: Config,
    findings: list,
    targets: list[ReviewTarget],
    repo_path: Path,
    skipped: list[str],
) -> None:
    """--suggest-fixes: an opt-in second pass over the findings that already
    came back from this run, asking whichever LLM tier is already configured
    (cloud preferred over local, matching how both tiers can run together
    elsewhere) for a short fix suggestion on each one that doesn't already
    have one -- see codecheck.suggest for why this is a narrower, cheaper ask
    than a full independent review.

    Only an OpenAIProtocolReviewer subclass (OpenAICompatibleCloudReviewer,
    LocalLLMReviewer) is usable here -- codecheck.suggest.generate_suggestions
    is written against that shared _get_client()/_resolved_base_url()/
    config.model interface. AnthropicCloudReviewer (cloud.provider="anthropic",
    the default) is a separate implementation with none of those, so it's
    deliberately excluded here rather than handed to generate_suggestions and
    crashing with an AttributeError -- confirmed as a real bug via Greptile
    review: the default cloud provider is exactly the one this would have
    crashed on.
    """
    if not suggest_fixes:
        return
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

    targets_by_path = {t.path: t for t in targets}
    exclude_checks = set(cfg.suggestions.exclude_checks)
    with console.status("[bold]Generating fix suggestions..."):
        count, suggest_skips = generate_suggestions(
            reviewer, findings, repo_path, targets_by_path, cfg.suggestions.max_per_run, exclude_checks
        )
    skipped.extend(suggest_skips)
    if count:
        noun = "suggestion" if count == 1 else "suggestions"
        console.print(f"[dim]Generated {count} fix {noun}.[/dim]")


def _connect_frappe_db(
    frappe_db_config: Path | None, untrusted: bool, skipped: list[str]
) -> FrappeDbConnection | None:
    """--frappe-db-config: opens a live connection to a Frappe site's database
    (for RULE-019 and any future DB-verified checks) if a path was given.
    Refuses outright when the review target is untrusted code (--repo-url or
    --pr, not a local --repo-path) -- running live DB queries derived from
    someone else's code while reviewing a fork/PR you don't control is a
    materially different risk than reviewing your own local bench, and this
    feature is only meant for the latter. A connection failure (bad path,
    wrong credentials, DB unreachable) is recorded as a skip and returns
    None, same as any other tier that couldn't run, rather than aborting the
    whole review.
    """
    if frappe_db_config is None:
        return None
    if untrusted:
        console.print(
            "[red]Error:[/red] --frappe-db-config can't be combined with --repo-url/--pr "
            "-- it runs live database queries derived from the reviewed code's own DocType/"
            "field names, which isn't safe to do against code you don't control. Use it "
            "with a local --repo-path only."
        )
        raise typer.Exit(code=2)
    try:
        return FrappeDbConnection.connect(frappe_db_config)
    except FrappeDbUnavailable as e:
        skipped.append(f"frappe_db: {e}")
        return None


@app.command()
def diff(
    repo_path: Path = typer.Option(Path("."), "--repo-path", help="Path to the git repo."),
    repo_url: Optional[str] = typer.Option(
        None, "--repo-url", help="Clone this URL to a temp dir and review it instead of --repo-path."
    ),
    branch: Optional[str] = typer.Option(
        None, "--branch", help="Branch to clone when using --repo-url (defaults to the remote's default branch). Ignored without --repo-url."
    ),
    base_ref: Optional[str] = typer.Option(
        None,
        "--base-ref",
        help="Ref to diff against. Defaults to 'main'. With --pr, defaults to the PR's actual base branch (via gh, if available) instead.",
    ),
    staged: bool = typer.Option(False, "--staged", help="Review staged changes instead of a base-ref diff."),
    pr: Optional[str] = typer.Option(
        None,
        "--pr",
        help=(
            "Review a GitHub PR — pass the full PR URL (e.g. "
            "https://github.com/org/repo/pull/123) to clone that repo and review it "
            "directly, or just a number to review a PR from --repo-path's existing "
            "'origin' remote. Mutually exclusive with --staged."
        ),
    ),
    config: Optional[Path] = typer.Option(None, "--config", help="Path to config.yaml."),
    cloud: bool = typer.Option(False, "--cloud", help="Enable the cloud LLM tier (Tier 3)."),
    local: bool = typer.Option(
        False, "--local", help="Enable the local LLM tier (Tier 2) — a local OpenAI-compatible server, e.g. LM Studio."
    ),
    force_cloud: bool = typer.Option(
        False, "--force-cloud", help="Bypass the cloud tier's file cap (cloud.audit_file_cap) for large diffs/PRs."
    ),
    force_local: bool = typer.Option(
        False,
        "--force-local",
        help="Skip the confirmation prompt when the local LLM tier would run on this machine (e.g. an LM Link remote couldn't be confirmed).",
    ),
    device: Optional[str] = typer.Option(
        None,
        "--device",
        help="Which device to use when local.model is loaded on more than one (e.g. 'local' or a device name from `lms link status`). Sets LM Studio's LM Link preferred device.",
    ),
    output_dir: Path = typer.Option(Path("./reports"), "--output-dir", help="Where JSON/markdown/docx reports land. Filenames are timestamped: <repo>[_pr<N>]_<mode>_<timestamp>.{json,md,docx}."),
    resume_from: Optional[Path] = typer.Option(
        None,
        "--resume-from",
        help=(
            "Path to a prior run's report.json. Files the cloud/local LLM tier already "
            "got a real result for there are skipped (not re-requested) and that prior "
            "result is reused — only files skipped last time (rate limit, transient "
            "error) get retried. Use this to make repeated retries against a rate-limited "
            "provider actually converge instead of re-hitting the same first few files "
            "every time."
        ),
    ),
    gate: Optional[str] = typer.Option(
        None, "--gate", help=f"Override thresholds.fail_on_severity via a named profile: {', '.join(_GATE_PROFILES)}."
    ),
    redact: bool = typer.Option(
        False, "--redact", help="Scrub locally-identifying details (e.g. your machine's absolute repo path) from the written reports before they're saved."
    ),
    suggest_fixes: bool = typer.Option(
        False,
        "--suggest-fixes",
        help=(
            "After the normal review, ask whichever LLM tier is enabled (--cloud/--local) "
            "for a short fix suggestion on findings that don't already have one. Capped at "
            "suggestions.max_per_run findings (highest severity first); see "
            "suggestions.exclude_checks to skip specific check IDs entirely."
        ),
    ),
    frappe_db_config: Optional[Path] = typer.Option(
        None,
        "--frappe-db-config",
        help=(
            "Path to a live Frappe site's site_config.json -- turns on RULE-019 "
            "(DocType field references verified against the real schema, not guessed "
            "statically). Read-only; credentials come from the file itself, never "
            "prompted for or stored. Refuses --repo-url/--pr (untrusted code)."
        ),
    ),
):
    """Review a git diff: staged changes, a base-ref...HEAD diff, or a GitHub PR
    (by full URL, or by number against --repo-path's existing 'origin')."""
    resolved_gate = _resolve_gate(gate)
    if staged and pr is not None:
        console.print("[red]Error:[/red] --staged and --pr are mutually exclusive.")
        raise typer.Exit(code=2)

    pr_number: Optional[int] = None
    if pr is not None:
        parsed = parse_pr_url(pr)
        if parsed is not None:
            parsed_repo_url, pr_number = parsed
            if repo_url and repo_url != parsed_repo_url:
                console.print(
                    f"[red]Error:[/red] --repo-url ({repo_url!r}) and the repo in "
                    f"--pr's URL ({parsed_repo_url!r}) don't match — pass one or the other."
                )
                raise typer.Exit(code=2)
            repo_url = parsed_repo_url
        else:
            try:
                pr_number = int(pr)
            except ValueError:
                console.print(
                    f"[red]Error:[/red] --pr must be a PR number or a full PR URL "
                    f"(e.g. https://github.com/org/repo/pull/123), got {pr!r}."
                )
                raise typer.Exit(code=2)

    start = time.monotonic()
    cfg = load_config(config)
    if cloud:
        cfg.cloud.enabled = True
    if local:
        cfg.local.enabled = True
    if resolved_gate:
        cfg.thresholds.fail_on_severity = resolved_gate
    repo_label = _repo_label(repo_path, repo_url)
    untrusted_source = repo_url is not None or pr_number is not None

    with ExitStack() as stack:
        skipped_early: list[str] = []
        frappe_db = _connect_frappe_db(frappe_db_config, untrusted_source, skipped_early)
        if frappe_db is not None:
            stack.callback(frappe_db.close)

        if repo_url:
            try:
                source_repo_path = stack.enter_context(cloned_repo(repo_url, branch=branch))
            except ValueError as e:
                console.print(f"[red]Error:[/red] {e}")
                raise typer.Exit(code=2)
        else:
            source_repo_path = repo_path.resolve()

        try:
            if pr_number is not None:
                review_repo_path, resolved_base_ref = stack.enter_context(
                    pr_worktree(source_repo_path, pr_number, base_ref)
                )
                targets = get_diff(review_repo_path, base_ref=resolved_base_ref, staged=False)
                report_base_ref = resolved_base_ref
                report_head_ref = f"pull/{pr_number}"
            else:
                review_repo_path = source_repo_path
                report_base_ref = None if staged else (base_ref or "main")
                targets = get_diff(
                    review_repo_path, base_ref=None if staged else report_base_ref, staged=staged
                )
                report_head_ref = None if staged else "HEAD"
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(code=2)

        if not targets:
            console.print("[yellow]No changed files found.[/yellow]")
            raise typer.Exit(code=0)

        if cfg.cloud.enabled:
            # A diff's size isn't inherently bounded when the diff/PR is
            # attacker-controlled (the cloud tier makes one API call per changed
            # file), so apply the same per-run file cap as `audit`.
            capped_count = exceeds_audit_cap(targets, cfg.cloud, force=force_cloud)
            if capped_count is not None:
                console.print(
                    f"[red]Refusing to run the cloud tier over {capped_count} changed files[/red] "
                    f"(cap is {cfg.cloud.audit_file_cap} — see cloud.audit_file_cap in config).\n"
                    f"A large diff/PR would make up to {capped_count} API calls. Re-run with "
                    f"--force-cloud to proceed anyway."
                )
                raise typer.Exit(code=2)

        results, tiers_run, skipped, file_hashes = _run_tiers(
            targets, review_repo_path, cfg, "diff",
            force_local=force_local, device=device, resume_from=resume_from, frappe_db=frappe_db,
        )
        skipped = skipped_early + skipped
        findings = aggregate(results)
        _maybe_suggest_fixes(suggest_fixes, cfg, findings, targets, review_repo_path, skipped)

        report = ReviewReport(
            repo_path=str(source_repo_path),
            mode="diff",
            base_ref=report_base_ref,
            head_ref=report_head_ref,
            generated_at=datetime.now(timezone.utc),
            tiers_run=tiers_run,
            findings=findings,
            files_reviewed=[t.path for t in targets],
            duration_seconds=time.monotonic() - start,
            skipped=skipped,
            file_hashes=file_hashes,
        )
        _finish(report, output_dir, cfg, repo_label, pr_number=pr_number, redact=redact)


@app.command()
def audit(
    repo_path: Path = typer.Option(Path("."), "--repo-path", help="Path to the git repo."),
    repo_url: Optional[str] = typer.Option(
        None, "--repo-url", help="Clone this URL to a temp dir and audit it instead of --repo-path."
    ),
    branch: Optional[str] = typer.Option(
        None, "--branch", help="Branch to clone when using --repo-url (defaults to the remote's default branch). Ignored without --repo-url."
    ),
    config: Optional[Path] = typer.Option(None, "--config", help="Path to config.yaml."),
    cloud: bool = typer.Option(False, "--cloud", help="Enable the cloud LLM tier (Tier 3)."),
    local: bool = typer.Option(
        False, "--local", help="Enable the local LLM tier (Tier 2) — a local OpenAI-compatible server, e.g. LM Studio."
    ),
    force_cloud: bool = typer.Option(
        False, "--force-cloud", help="Bypass the cloud tier's per-audit file cap (cloud.audit_file_cap)."
    ),
    force_local: bool = typer.Option(
        False,
        "--force-local",
        help="Skip the confirmation prompt when the local LLM tier would run on this machine (e.g. an LM Link remote couldn't be confirmed).",
    ),
    device: Optional[str] = typer.Option(
        None,
        "--device",
        help="Which device to use when local.model is loaded on more than one (e.g. 'local' or a device name from `lms link status`). Sets LM Studio's LM Link preferred device.",
    ),
    output_dir: Path = typer.Option(Path("./reports"), "--output-dir", help="Where JSON/markdown/docx reports land. Filenames are timestamped: <repo>[_pr<N>]_<mode>_<timestamp>.{json,md,docx}."),
    resume_from: Optional[Path] = typer.Option(
        None,
        "--resume-from",
        help=(
            "Path to a prior run's report.json. Files the cloud/local LLM tier already "
            "got a real result for there are skipped (not re-requested) and that prior "
            "result is reused — only files skipped last time (rate limit, transient "
            "error) get retried. Use this to make repeated retries against a rate-limited "
            "provider actually converge instead of re-hitting the same first few files "
            "every time."
        ),
    ),
    gate: Optional[str] = typer.Option(
        None, "--gate", help=f"Override thresholds.fail_on_severity via a named profile: {', '.join(_GATE_PROFILES)}."
    ),
    redact: bool = typer.Option(
        False, "--redact", help="Scrub locally-identifying details (e.g. your machine's absolute repo path) from the written reports before they're saved."
    ),
    suggest_fixes: bool = typer.Option(
        False,
        "--suggest-fixes",
        help=(
            "After the normal review, ask whichever LLM tier is enabled (--cloud/--local) "
            "for a short fix suggestion on findings that don't already have one. Capped at "
            "suggestions.max_per_run findings (highest severity first); see "
            "suggestions.exclude_checks to skip specific check IDs entirely."
        ),
    ),
    frappe_db_config: Optional[Path] = typer.Option(
        None,
        "--frappe-db-config",
        help=(
            "Path to a live Frappe site's site_config.json -- turns on RULE-019 "
            "(DocType field references verified against the real schema, not guessed "
            "statically). Read-only; credentials come from the file itself, never "
            "prompted for or stored. Refuses --repo-url (untrusted code)."
        ),
    ),
):
    """Audit the whole repo (every tracked file, plus untracked-but-not-ignored
    files) instead of just a diff."""
    resolved_gate = _resolve_gate(gate)
    start = time.monotonic()
    cfg = load_config(config)
    if cloud:
        cfg.cloud.enabled = True
    if local:
        cfg.local.enabled = True
    if resolved_gate:
        cfg.thresholds.fail_on_severity = resolved_gate
    repo_label = _repo_label(repo_path, repo_url)
    untrusted_source = repo_url is not None

    with ExitStack() as stack:
        skipped_early: list[str] = []
        frappe_db = _connect_frappe_db(frappe_db_config, untrusted_source, skipped_early)
        if frappe_db is not None:
            stack.callback(frappe_db.close)

        if repo_url:
            try:
                effective_repo_path = stack.enter_context(cloned_repo(repo_url, branch=branch))
            except ValueError as e:
                console.print(f"[red]Error:[/red] {e}")
                raise typer.Exit(code=2)
        else:
            effective_repo_path = repo_path.resolve()

        targets = get_repo_files(effective_repo_path)
        if not targets:
            console.print("[yellow]No files found to audit.[/yellow]")
            raise typer.Exit(code=0)

        if cfg.cloud.enabled:
            capped_count = exceeds_audit_cap(targets, cfg.cloud, force=force_cloud)
            if capped_count is not None:
                console.print(
                    f"[red]Refusing to run the cloud tier over {capped_count} files[/red] "
                    f"(cap is {cfg.cloud.audit_file_cap} — see cloud.audit_file_cap in config).\n"
                    f"This would make up to {capped_count} API calls. Re-run with --force-cloud "
                    f"to proceed anyway, or lower the scope."
                )
                raise typer.Exit(code=2)

        console.print(f"[dim]Auditing {len(targets)} file(s) in {effective_repo_path}[/dim]")

        results, tiers_run, skipped, file_hashes = _run_tiers(
            targets, effective_repo_path, cfg, "audit",
            force_local=force_local, device=device, resume_from=resume_from, frappe_db=frappe_db,
        )
        skipped = skipped_early + skipped
        findings = aggregate(results)
        _maybe_suggest_fixes(suggest_fixes, cfg, findings, targets, effective_repo_path, skipped)

        report = ReviewReport(
            repo_path=str(effective_repo_path),
            mode="audit",
            base_ref=None,
            head_ref=None,
            generated_at=datetime.now(timezone.utc),
            tiers_run=tiers_run,
            findings=findings,
            files_reviewed=[t.path for t in targets],
            duration_seconds=time.monotonic() - start,
            skipped=skipped,
            file_hashes=file_hashes,
        )
        _finish(report, output_dir, cfg, repo_label, redact=redact)


def _load_report_file(path: Path) -> ReviewReport:
    try:
        data = json.loads(path.read_text())
    except OSError as e:
        console.print(f"[red]Error:[/red] could not read {path}: {e}")
        raise typer.Exit(code=2)
    except json.JSONDecodeError as e:
        console.print(f"[red]Error:[/red] {path} is not valid JSON: {e}")
        raise typer.Exit(code=2)
    if not isinstance(data, dict):
        console.print(f"[red]Error:[/red] {path} does not contain a JSON object.")
        raise typer.Exit(code=2)
    return ReviewReport.from_dict(data)


@app.command()
def render(
    report_json: Path = typer.Argument(
        ..., help="Path to a prior run's .json report (see 'Report filenames' in the docs)."
    ),
    output_dir: Path = typer.Option(
        Path("./reports"), "--output-dir", help="Where the re-rendered reports land."
    ),
    redact: bool = typer.Option(
        False, "--redact", help="Scrub locally-identifying details from the re-rendered reports."
    ),
):
    """Re-render a prior run's .json report as markdown/docx, without re-running
    any checks. Useful after upgrading codecheck (to get the newer reporter
    output from an old report) or to produce a --redact copy of a report you
    already have, without a full re-review."""
    report = _load_report_file(report_json)
    # Derive the filename label from the real repo_path before redacting --
    # otherwise a --redact render's own filename would be built from the
    # placeholder text instead of an actual repo name.
    repo_label = _sanitize_slug(Path(report.repo_path).name) or "repo"
    if redact:
        report = redact_report(report)
    json_path, md_path, docx_path = _write_reports(report, output_dir, repo_label)
    console.print(f"[dim]Reports written to {json_path}, {md_path}, and {docx_path}[/dim]")
    raise typer.Exit(code=0)


@app.command()
def compare(
    old_report: Path = typer.Argument(..., help="Path to the earlier run's .json report (the baseline)."),
    new_report: Path = typer.Argument(..., help="Path to the later run's .json report."),
    config: Optional[Path] = typer.Option(None, "--config", help="Path to config.yaml (for thresholds.fail_on_severity)."),
    gate: Optional[str] = typer.Option(
        None, "--gate", help=f"Override thresholds.fail_on_severity via a named profile: {', '.join(_GATE_PROFILES)}."
    ),
):
    """Compare two prior .json reports (e.g. a baseline audit vs. a later one
    of the same repo) and show which findings are newly introduced vs.
    resolved since the baseline. Exits 1 if any newly introduced finding is
    at or above thresholds.fail_on_severity -- wire this into CI to catch a
    codebase getting worse over time, not just a single run's snapshot."""
    old = _load_report_file(old_report)
    new = _load_report_file(new_report)
    added, resolved = diff_reports(old, new)

    cfg = load_config(config)
    resolved_gate = _resolve_gate(gate)
    if resolved_gate:
        cfg.thresholds.fail_on_severity = resolved_gate

    console.print(f"[dim]Baseline: {old_report} ({len(old.findings)} finding(s))[/dim]")
    console.print(f"[dim]Compared: {new_report} ({len(new.findings)} finding(s))[/dim]\n")

    if added:
        console.print(f"[bold red]{len(added)} newly introduced finding(s):[/bold red]")
        for f in sorted(added, key=lambda x: (-x.severity.rank, x.file, x.line_start)):
            console.print(f"  [{f.severity.value.upper()}] {f.file}:{f.line_start} {f.check_id} — {f.title}")
    else:
        console.print("[dim]No newly introduced findings.[/dim]")

    console.print()
    if resolved:
        console.print(f"[bold green]{len(resolved)} resolved finding(s):[/bold green]")
        for f in sorted(resolved, key=lambda x: (-x.severity.rank, x.file, x.line_start)):
            console.print(f"  [{f.severity.value.upper()}] {f.file}:{f.line_start} {f.check_id} — {f.title}")
    else:
        console.print("[dim]No resolved findings.[/dim]")

    fail_threshold = Severity(cfg.thresholds.fail_on_severity)
    if any(f.severity >= fail_threshold for f in added):
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
