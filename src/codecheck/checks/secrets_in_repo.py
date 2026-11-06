"""RULE-035: flag an `.env` file present in the checkout that isn't covered
by any `.gitignore`. A presence check, not a tracked-status check.

A repo-wide filesystem scan, not a per-file content check -- implemented as
its own SubRunner (duck-typed, to avoid a circular import with
rules_engine.py). Doesn't detect secrets inside file content -- RULE-009/
RULE-016 already cover that."""

from __future__ import annotations

from pathlib import Path

import pathspec

from codecheck.models import Finding, ReviewTarget, Severity


def _read_gitignore_patterns(gitignore_path: Path) -> list[str]:
    if not gitignore_path.is_file():
        return []
    try:
        return gitignore_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def _git_root(repo_path: Path) -> Path:
    """Nearest ancestor of repo_path (itself included) holding a .git entry; repo_path
    itself when there is none, so a scan outside a repo behaves as it always did."""
    for candidate in (repo_path, *repo_path.parents):
        if (candidate / ".git").exists():
            return candidate
    return repo_path


def _is_ignored(repo_path: Path, env_path: Path) -> bool:
    """Whether git would ignore `env_path`, with real gitignore semantics (wildcards,
    directory anchoring, `!` negation) via `pathspec`.

    Every `.gitignore` from the git root down to `env_path`'s own directory is
    consulted -- not just repo_path's, since `--repo-path` may select a subdirectory
    of the repo whose ancestors' rules still apply -- and each is matched against
    the path relative to its own directory, the frame git uses. They are applied
    root first, so a deeper file's verdict overrides a shallower one's, as in git:
    a root `.env*` ignore is lifted again by an `app/.gitignore` containing
    `!.env.production`. A file no pattern mentions is not ignored.

    Not modelled: an ignored *parent directory* (git then never looks inside it, so
    a `!` cannot re-include the file). Treating that file as not ignored errs toward
    reporting it, the safe direction for a check whose job is not missing a secret.
    """
    root = _git_root(repo_path)
    directories = [env_path.parent]
    while directories[-1] != root and directories[-1] != directories[-1].parent:
        directories.append(directories[-1].parent)

    ignored = False
    for directory in reversed(directories):  # git root first, env_path's directory last
        patterns = _read_gitignore_patterns(directory / ".gitignore")
        if not patterns:
            continue
        rel_to_here = env_path.relative_to(directory).as_posix()
        verdict = pathspec.PathSpec.from_lines("gitignore", patterns).check_file(rel_to_here).include
        if verdict is not None:
            ignored = verdict
    return ignored


# .env.example and friends are meant to be committed -- they hold placeholders, not secrets.
_TEMPLATE_SUFFIXES = frozenset({"example", "sample", "template", "dist", "defaults"})


def _is_env_file_name(name: str) -> bool:
    """`.env` itself, or a variant such as `.env.local` / `.env.production`."""
    if name == ".env":
        return True
    return name.startswith(".env.") and name.rsplit(".", 1)[-1] not in _TEMPLATE_SUFFIXES


class SecretsInRepoRunner:
    check_id = "RULE-035"
    title = ".env file present and not covered by .gitignore"
    severity = Severity.HIGH
    name = "secrets_in_repo"

    def is_available(self, repo_path: Path) -> tuple[bool, str | None]:
        return True, None

    def run(self, targets: list[ReviewTarget], repo_path: Path) -> list[Finding]:
        if not repo_path.is_dir():
            return []
        findings = []
        for env_path in sorted(repo_path.rglob(".env*")):
            if not _is_env_file_name(env_path.name) or not env_path.is_file():
                continue
            if _is_ignored(repo_path, env_path):
                continue
            rel_path = env_path.relative_to(repo_path).as_posix()
            findings.append(
                Finding(
                    check_id=self.check_id,
                    tier="rules",
                    source="house",
                    severity=self.severity,
                    title=self.title,
                    explanation=(
                        f"'{rel_path}' is present in this checkout and not matched by any "
                        ".gitignore pattern. If it's tracked by git, any secret inside it "
                        "(API keys, DB passwords) is in repo history for anyone with access, "
                        "not just this machine -- add it to .gitignore and rotate any secret "
                        "it contains if it's already been committed."
                    ),
                    file=rel_path,
                    line_start=1,
                    line_end=1,
                )
            )
        return findings
