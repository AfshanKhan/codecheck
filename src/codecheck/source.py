"""Target acquisition seam: encapsulates how targets (and their working directory)
are obtained, whether from a local git diff, a remote GitHub PR, a full repository scan,
or in-memory test fakes. Guarantees clean resource management upon exit."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import git

from codecheck.diff import get_diff
from codecheck.github_source import cloned_repo, parse_pr_url, pr_worktree, resolve_pr_base_ref
from codecheck.models import ReviewTarget
from codecheck.repo_scan import get_repo_files


def _sanitize_slug(value: str) -> str:
    return "".join(c for c in value if c.isalnum() or c in ("-", "_"))


def sanitize_repo_url(repo_url: str) -> str:
    """Strip userinfo/query/fragment out of a URL before it is stored or displayed."""
    parts = urlsplit(repo_url)
    if not parts.scheme:
        return repo_url
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def compute_repo_label(repo_path: Path, repo_url: str | None = None) -> str:
    """A short, filesystem-safe name for the reviewed repo, used in report filenames."""
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


@dataclass
class AcquiredWorkspace:
    """Workspace context yielded by a ReviewSource.acquire() context manager."""

    repo_path: Path
    targets: list[ReviewTarget]
    repo_label: str
    mode: str  # "diff" | "audit" | "scripts"
    base_ref: str | None = None
    head_ref: str | None = None
    untrusted: bool = False
    pr_number: int | None = None
    frappe_site_name: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class ReviewSource(ABC):
    """Abstract seam for target acquisition."""

    @contextmanager
    @abstractmethod
    def acquire(self) -> Generator[AcquiredWorkspace, None, None]:
        """Acquire the target files and workspace directory, guaranteeing cleanup on exit."""
        ...


class GitDiffSource(ReviewSource):
    """Acquires review targets from a local git diff."""

    def __init__(
        self,
        repo_path: Path | None = None,
        base_ref: str | None = None,
        staged: bool = False,
        repo_label: str | None = None,
        repo_url: str | None = None,
        branch: str | None = None,
    ) -> None:
        self.repo_path = Path(repo_path) if repo_path else None
        self.base_ref = base_ref
        self.staged = staged
        self.repo_url = repo_url
        self.branch = branch
        self._custom_repo_label = repo_label

    @contextmanager
    def acquire(self) -> Generator[AcquiredWorkspace, None, None]:
        with ExitStack() as stack:
            untrusted = False
            if self.repo_url:
                resolved_repo = stack.enter_context(cloned_repo(self.repo_url, branch=self.branch))
                untrusted = True
            elif self.repo_path:
                resolved_repo = self.repo_path.resolve()
            else:
                resolved_repo = Path.cwd().resolve()

            label = self._custom_repo_label or compute_repo_label(resolved_repo, self.repo_url)
            effective_base = self.base_ref
            if not self.staged and not effective_base:
                effective_base = "main"

            targets = get_diff(
                resolved_repo,
                base_ref=None if self.staged else effective_base,
                staged=self.staged,
            )

            head_ref = None
            if not self.staged:
                try:
                    repo = git.Repo(resolved_repo, search_parent_directories=True)
                    if not repo.head.is_detached:
                        head_ref = repo.active_branch.name
                    else:
                        head_ref = repo.head.commit.hexsha[:8]
                except Exception:
                    head_ref = "HEAD"

            yield AcquiredWorkspace(
                repo_path=resolved_repo,
                targets=targets,
                repo_label=label,
                mode="diff",
                base_ref=None if self.staged else effective_base,
                head_ref=head_ref,
                untrusted=untrusted,
            )


class GitHubPrSource(ReviewSource):
    """Acquires review targets from a GitHub PR (local worktree or remote clone)."""

    def __init__(
        self,
        pr: int | str,
        repo_path: Path | None = None,
        repo_url: str | None = None,
        base_ref: str | None = None,
        repo_label: str | None = None,
    ) -> None:
        if isinstance(pr, str) and (pr_parsed := parse_pr_url(pr)):
            self.repo_url = pr_parsed[0]
            self.pr_number = pr_parsed[1]
        else:
            self.repo_url = repo_url
            self.pr_number = int(pr)

        self.repo_path = Path(repo_path) if repo_path else None
        self.base_ref = base_ref
        self._custom_repo_label = repo_label

    @contextmanager
    def acquire(self) -> Generator[AcquiredWorkspace, None, None]:
        with ExitStack() as stack:
            untrusted = False
            if self.repo_url:
                source_repo_path = stack.enter_context(cloned_repo(self.repo_url))
                untrusted = True
            elif self.repo_path:
                source_repo_path = self.repo_path.resolve()
            else:
                source_repo_path = Path.cwd().resolve()

            label = self._custom_repo_label or compute_repo_label(source_repo_path, self.repo_url)
            resolved_base_ref = self.base_ref or resolve_pr_base_ref(source_repo_path, self.pr_number) or "main"

            worktree_dir, base_local_ref = stack.enter_context(
                pr_worktree(source_repo_path, self.pr_number, resolved_base_ref)
            )

            targets = get_diff(worktree_dir, base_ref=base_local_ref, staged=False)

            yield AcquiredWorkspace(
                repo_path=worktree_dir,
                targets=targets,
                repo_label=label,
                mode="diff",
                base_ref=resolved_base_ref,
                head_ref=f"pr-{self.pr_number}",
                untrusted=untrusted,
                pr_number=self.pr_number,
            )


class RepoAuditSource(ReviewSource):
    """Acquires all files in a repository for audit mode."""

    def __init__(
        self,
        repo_path: Path | None = None,
        repo_url: str | None = None,
        branch: str | None = None,
        repo_label: str | None = None,
    ) -> None:
        self.repo_path = Path(repo_path) if repo_path else None
        self.repo_url = repo_url
        self.branch = branch
        self._custom_repo_label = repo_label

    @contextmanager
    def acquire(self) -> Generator[AcquiredWorkspace, None, None]:
        with ExitStack() as stack:
            untrusted = False
            if self.repo_url:
                effective_path = stack.enter_context(cloned_repo(self.repo_url, branch=self.branch))
                untrusted = True
            elif self.repo_path:
                effective_path = self.repo_path.resolve()
            else:
                effective_path = Path.cwd().resolve()

            label = self._custom_repo_label or compute_repo_label(effective_path, self.repo_url)
            targets = get_repo_files(effective_path)

            head_ref = None
            try:
                repo = git.Repo(effective_path, search_parent_directories=True)
                if not repo.head.is_detached:
                    head_ref = repo.active_branch.name
                else:
                    head_ref = repo.head.commit.hexsha[:8]
            except Exception:
                pass

            yield AcquiredWorkspace(
                repo_path=effective_path,
                targets=targets,
                repo_label=label,
                mode="audit",
                head_ref=self.branch or head_ref,
                untrusted=untrusted,
            )


class InMemorySource(ReviewSource):
    """In-memory source adapter for deterministic unit/integration testing."""

    def __init__(
        self,
        targets: list[ReviewTarget],
        repo_path: Path | None = None,
        repo_label: str = "in-memory",
        mode: str = "audit",
        base_ref: str | None = None,
        head_ref: str | None = None,
        untrusted: bool = False,
    ) -> None:
        self.targets = targets
        self.repo_path = repo_path or Path("/test/repo")
        self.repo_label = repo_label
        self.mode = mode
        self.base_ref = base_ref
        self.head_ref = head_ref
        self.untrusted = untrusted

    @contextmanager
    def acquire(self) -> Generator[AcquiredWorkspace, None, None]:
        yield AcquiredWorkspace(
            repo_path=self.repo_path,
            targets=list(self.targets),
            repo_label=self.repo_label,
            mode=self.mode,
            base_ref=self.base_ref,
            head_ref=self.head_ref,
            untrusted=self.untrusted,
        )
