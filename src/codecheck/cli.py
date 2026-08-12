from __future__ import annotations

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
from codecheck.diff import get_diff
from codecheck.github_source import cloned_repo, parse_pr_url, pr_worktree
from codecheck.lm_link import resolve_model_location, set_preferred_device
from codecheck.models import ReviewReport, ReviewTarget, Severity
from codecheck.repo_scan import get_repo_files
from codecheck.reporters.console import print_report
from codecheck.reporters.json_report import write_json_report
from codecheck.reporters.markdown_report import write_markdown_report
from codecheck.reviewers.cloud_llm import build_cloud_reviewer, exceeds_audit_cap
from codecheck.reviewers.local_llm import LocalLLMReviewer
from codecheck.reviewers.rules_engine import RulesEngineReviewer

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


def _run_tiers(
    targets: list[ReviewTarget],
    repo_path: Path,
    cfg: Config,
    force_local: bool = False,
    device: str | None = None,
) -> tuple[dict[str, list], list[str], list[str]]:
    """Runs the rules tier and (if available) the local and cloud LLM tiers over
    the given targets. Returns (results_by_tier, tiers_run, skip_reasons).
    """
    results: dict[str, list] = {}
    tiers_run: list[str] = []
    skipped: list[str] = []

    rules_reviewer = RulesEngineReviewer(cfg.rules)
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
        with console.status("[bold]Running local LLM review..."):
            results["local_llm"] = local_reviewer.review(targets, repo_path)
        tiers_run.append("local_llm")
        skipped.extend(f"local_llm: {path}: {reason}" for path, reason in local_reviewer.skipped_files)
    elif cfg.local.enabled and reason:
        skipped.append(f"local tier: {reason}")

    cloud_reviewer = build_cloud_reviewer(cfg.cloud)
    available, reason = cloud_reviewer.is_available(repo_path)
    if available:
        with console.status("[bold]Running cloud LLM review..."):
            results["cloud_llm"] = cloud_reviewer.review(targets, repo_path)
        tiers_run.append("cloud_llm")
        skipped.extend(f"cloud_llm: {path}: {reason}" for path, reason in cloud_reviewer.skipped_files)
    elif cfg.cloud.enabled and reason:
        skipped.append(f"cloud tier: {reason}")

    return results, tiers_run, skipped


def _finish(report: ReviewReport, output_dir: Path, cfg: Config) -> None:
    console.print()
    print_report(report, console)

    output_dir.mkdir(parents=True, exist_ok=True)
    write_json_report(report, output_dir / "report.json")
    write_markdown_report(report, output_dir / "report.md")
    console.print(f"\n[dim]Reports written to {output_dir}/report.json and {output_dir}/report.md[/dim]")

    fail_threshold = Severity(cfg.thresholds.fail_on_severity)
    if report.findings_at_or_above(fail_threshold):
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


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
    output_dir: Path = typer.Option(Path("./reports"), "--output-dir", help="Where JSON/markdown reports land."),
):
    """Review a git diff: staged changes, a base-ref...HEAD diff, or a GitHub PR
    (by full URL, or by number against --repo-path's existing 'origin')."""
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

    with ExitStack() as stack:
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

        results, tiers_run, skipped = _run_tiers(
            targets, review_repo_path, cfg, force_local=force_local, device=device
        )
        findings = aggregate(results)

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
        )
        _finish(report, output_dir, cfg)


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
    output_dir: Path = typer.Option(Path("./reports"), "--output-dir", help="Where JSON/markdown reports land."),
):
    """Audit the whole repo (every tracked file, plus untracked-but-not-ignored
    files) instead of just a diff."""
    start = time.monotonic()
    cfg = load_config(config)
    if cloud:
        cfg.cloud.enabled = True
    if local:
        cfg.local.enabled = True

    with ExitStack() as stack:
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

        results, tiers_run, skipped = _run_tiers(
            targets, effective_repo_path, cfg, force_local=force_local, device=device
        )
        findings = aggregate(results)

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
        )
        _finish(report, output_dir, cfg)


if __name__ == "__main__":
    app()
