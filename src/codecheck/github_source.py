"""Sources a review target from a GitHub PR number or a remote repo URL, without
ever touching the caller's current checkout.

- `pr_worktree` fetches a PR's head commit via GitHub's `refs/pull/<n>/head` ref
  (works against any GitHub repo, no API token needed beyond whatever `origin`
  already uses) into an isolated `git worktree`, so the user's current branch and
  working directory are never switched or disturbed. Cleans up the worktree and
  the temporary refs it created on exit.
- `cloned_repo` clones a URL to a temp directory and removes it on exit.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import git

# Matches a PR URL like https://github.com/org/repo/pull/123, optionally with
# a trailing path (/files, /commits, a trailing slash) -- GitHub Enterprise
# hosts too, since only the /pull/<n> shape is host-specific, not github.com
# itself. Group 1 is the repo's clone URL, group 2 is the PR number.
_PR_URL_RE = re.compile(r"^(https?://[^/\s]+/[^/\s]+/[^/\s]+?)(?:\.git)?/pull/(\d+)(?:/.*)?/?$")


def parse_pr_url(value: str) -> tuple[str, int] | None:
    """Parses a full PR URL into (repo_clone_url, pr_number). Returns None if
    value doesn't look like one -- callers should then try treating it as a
    bare PR number against an already-known repo instead.
    """
    match = _PR_URL_RE.match(value.strip())
    if not match:
        return None
    repo_url, number = match.groups()
    return repo_url, int(number)


def resolve_pr_base_ref(repo_path: Path, pr_number: int) -> str | None:
    """Best-effort: ask the gh CLI what the PR's base branch is. Returns None if
    gh isn't installed, isn't authenticated, or the lookup fails for any reason —
    callers should fall back to an explicit --base-ref or "main".
    """
    if shutil.which("gh") is None:
        return None
    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--json", "baseRefName", "-q", ".baseRefName"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    base_ref = result.stdout.strip()
    return base_ref or None


@contextmanager
def pr_worktree(
    repo_path: Path, pr_number: int, base_ref_override: str | None
) -> Iterator[tuple[Path, str]]:
    """Fetch PR #pr_number from the 'origin' remote and check it out into an
    isolated worktree. Yields (worktree_path, base_ref_to_diff_against).

    base_ref_override, if given, is used as-is instead of auto-resolving the
    PR's actual base branch via `gh`.
    """
    repo = git.Repo(repo_path, search_parent_directories=True)
    pr_ref = f"refs/codecheck/pr-{pr_number}"
    base_ref_name = base_ref_override or resolve_pr_base_ref(repo_path, pr_number) or "main"
    base_local_ref = f"refs/codecheck/base-{pr_number}"

    try:
        repo.git.fetch("--", "origin", f"pull/{pr_number}/head:{pr_ref}")
    except git.GitCommandError as e:
        raise ValueError(f"Could not fetch PR #{pr_number} from origin: {e}") from e

    try:
        repo.git.fetch("--", "origin", f"{base_ref_name}:{base_local_ref}")
    except git.GitCommandError as e:
        raise ValueError(f"Could not fetch base ref {base_ref_name!r} from origin: {e}") from e

    worktree_dir = Path(tempfile.mkdtemp(prefix=f"codecheck-pr-{pr_number}-"))
    try:
        repo.git.worktree("add", "--detach", str(worktree_dir), pr_ref)
    except git.GitCommandError as e:
        shutil.rmtree(worktree_dir, ignore_errors=True)
        _delete_ref(repo, pr_ref)
        _delete_ref(repo, base_local_ref)
        raise ValueError(f"Could not create worktree for PR #{pr_number}: {e}") from e

    try:
        yield worktree_dir, base_local_ref
    finally:
        try:
            repo.git.worktree("remove", "--force", str(worktree_dir))
        except git.GitCommandError:
            pass
        shutil.rmtree(worktree_dir, ignore_errors=True)
        _delete_ref(repo, pr_ref)
        _delete_ref(repo, base_local_ref)


def _delete_ref(repo: git.Repo, ref: str) -> None:
    try:
        repo.git.update_ref("-d", ref)
    except git.GitCommandError:
        pass


def _validate_clone_url(repo_url: str) -> None:
    """Reject repo_url values that would let git do something other than a plain
    clone. A leading '-' is argument injection into `git clone` (e.g. a URL of
    `-oProxyCommand=...` parsed as a flag, not a repo). '::' is git's
    transport-helper syntax (`ext::sh -c '...'`, `fd::...`) and is how a crafted
    URL achieves arbitrary command execution -- confirmed as a real vector since
    repo_url here can come from a caller-supplied string with no other checks.
    Plain local filesystem paths and https/http/ssh/git URLs are all still
    allowed; only these two attack shapes are blocked.
    """
    if not repo_url or repo_url.startswith("-"):
        raise ValueError(f"Invalid repo URL {repo_url!r}: must not start with '-'")
    # git's "<transport>::<address>" remote-helper syntax (ext::, fd::) can run
    # arbitrary commands. Reject only that shape -- a '::' with no '/' before it
    # -- so a legitimate URL that merely contains '::' in its authority (e.g. an
    # IPv6 literal like https://[::1]/repo, where the '::' comes after '//') is
    # still allowed.
    marker = repo_url.find("::")
    if marker != -1 and "/" not in repo_url[:marker]:
        raise ValueError(
            f"Invalid repo URL {repo_url!r}: git transport-helper syntax ('::') is not allowed"
        )


@contextmanager
def cloned_repo(repo_url: str, branch: str | None = None) -> Iterator[Path]:
    """Clone repo_url to a temp directory, yield its path, remove it on exit.
    If branch is given, clones that branch specifically (--single-branch, so
    only that branch's history is fetched) instead of the remote's default.
    """
    _validate_clone_url(repo_url)
    if branch is not None and branch.startswith("-"):
        raise ValueError(f"Invalid branch {branch!r}: must not start with '-'")

    cmd = ["git", "clone"]
    if branch:
        cmd += ["--branch", branch, "--single-branch"]
    cmd += ["--", repo_url]

    tmp_dir = Path(tempfile.mkdtemp(prefix="codecheck-clone-"))
    try:
        subprocess.run(
            [*cmd, str(tmp_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise ValueError(f"Could not clone {repo_url!r}: {e.stderr}") from e

    try:
        yield tmp_dir
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
