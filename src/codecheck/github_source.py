"""Sources a review target from a GitHub PR number or a remote repo URL, without
ever touching the caller's current checkout.

- `pr_worktree` fetches a PR's head commit via GitHub's `refs/pull/<n>/head` ref
  (works against any GitHub repo, no API token needed beyond whatever `origin`
  already uses) into an isolated `git worktree`, so the user's current branch and
  working directory are never switched or disturbed. Cleans up the worktree and
  the temporary refs it created on exit.
- `cloned_repo` clones a URL to a temp directory and removes it on exit.

Private repos: whatever git credential setup already works on this machine
(SSH key, `gh auth login`, `.netrc`, a credential helper) is always tried
first, for free -- codecheck does nothing extra in that case. Only if that
fails with what looks like an auth error, and we're at an interactive
terminal, do we prompt for a username/token and retry -- see
_try_with_credential_retry. The credentials are never written to the URL,
never land in the cloned repo's .git/config, and are never persisted to
disk: they're handed to git for that one operation only via a short-lived
GIT_ASKPASS script + environment variables.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
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


_MAX_CREDENTIAL_PROMPTS = 3

_AUTH_ERROR_MARKERS = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "permission denied (publickey)",
    "not found",  # git's actual message is "repository '<url>' not found" --
    # GitHub/GitLab return this for both "doesn't exist" and "private, no
    # access" deliberately, so a token guess can't be used to enumerate
    # private repo names. Treated as possibly-auth here.
    "invalid username or password",
    "403",
)


def _looks_like_auth_error(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _AUTH_ERROR_MARKERS)


def _prompt_credentials(repo_url: str, attempt_num: int, max_attempts: int) -> tuple[str, str] | None:
    """Returns None if the user gives up (empty input, Ctrl-C, or EOF e.g. from
    a script piping /dev/null) -- callers should treat that as "no credentials
    available" rather than retrying forever.
    """
    print(
        f"Authentication required for {repo_url!r} (attempt {attempt_num}/{max_attempts}).",
        file=sys.stderr,
    )
    try:
        username = input("  Username: ").strip()
        token = getpass.getpass("  Token/Password: ")
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)  # move off the prompt line
        return None
    if not username or not token:
        return None
    return username, token


@contextmanager
def _askpass_env(username: str, token: str) -> Iterator[dict[str, str]]:
    """A GIT_ASKPASS script that hands git the given credentials for exactly
    one operation. Deliberately not the "https://user:token@host/..." URL
    form: that leaks the token into `ps` output for every process on the
    machine while the command runs, and git bakes it into the resulting
    repo's .git/config on disk. This keeps the token out of argv and out of
    any file -- it only ever exists in this process's environment and the
    child git process's, for the duration of this context manager.
    """
    fd, path = tempfile.mkstemp(prefix="codecheck-askpass-", suffix=".sh")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(
                "#!/bin/sh\n"
                'case "$1" in\n'
                '  *sername*) printf "%s" "$CODECHECK_GIT_USERNAME" ;;\n'
                '  *) printf "%s" "$CODECHECK_GIT_TOKEN" ;;\n'
                "esac\n"
            )
        os.chmod(path, 0o700)
        env = dict(os.environ)
        env["GIT_ASKPASS"] = path
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["CODECHECK_GIT_USERNAME"] = username
        env["CODECHECK_GIT_TOKEN"] = token
        yield env
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _try_with_credential_retry(
    attempt: Callable[[dict[str, str] | None], tuple[bool, str]],
    repo_url_for_prompt: str,
) -> None:
    """attempt(extra_env) should perform one git operation and return
    (succeeded, stderr). Called first with extra_env=None -- whatever
    credentials git already has configured (SSH key, `gh auth login`,
    `.netrc`, a credential helper) are tried for free there, exactly as a
    plain `git clone`/`git fetch` would behave. Only if that fails with what
    looks like an auth error do we prompt interactively for a username/token
    and retry via _askpass_env, up to _MAX_CREDENTIAL_PROMPTS times.

    Never prompts outside an interactive terminal (CI, cron, piped input) --
    fails immediately with the original error instead of hanging forever
    waiting for input nobody can provide.

    Raises ValueError with a clear final message if every attempt fails.
    """
    ok, stderr = attempt(None)
    if ok:
        return
    if not _looks_like_auth_error(stderr):
        raise ValueError(stderr)
    if not sys.stdin.isatty():
        raise ValueError(
            f"{stderr}\n(Not prompting for credentials: no interactive terminal. "
            f"Set up git credentials for {repo_url_for_prompt!r} first -- an SSH key, "
            f"`gh auth login`, or a credential helper.)"
        )

    for attempt_num in range(1, _MAX_CREDENTIAL_PROMPTS + 1):
        creds = _prompt_credentials(repo_url_for_prompt, attempt_num, _MAX_CREDENTIAL_PROMPTS)
        if creds is None:
            raise ValueError(f"Repository not accessible: {repo_url_for_prompt!r} (no credentials provided).")
        username, token = creds
        with _askpass_env(username, token) as env:
            ok, stderr = attempt(env)
        if ok:
            return

    raise ValueError(
        f"Repository not accessible: {repo_url_for_prompt!r} "
        f"(wrong credentials after {_MAX_CREDENTIAL_PROMPTS} attempts)."
    )


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


def _origin_url_for_prompt(repo: git.Repo) -> str:
    try:
        return repo.remotes.origin.url
    except (AttributeError, ValueError):
        return "origin"


def _fetch_with_credential_retry(repo: git.Repo, refspec: str, repo_url_for_prompt: str) -> None:
    def _attempt(extra_env: dict[str, str] | None) -> tuple[bool, str]:
        try:
            if extra_env is None:
                repo.git.fetch("--", "origin", refspec)
            else:
                with repo.git.custom_environment(**extra_env):
                    repo.git.fetch("--", "origin", refspec)
            return True, ""
        except git.GitCommandError as e:
            return False, str(e)

    _try_with_credential_retry(_attempt, repo_url_for_prompt)


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
    origin_url = _origin_url_for_prompt(repo)

    try:
        _fetch_with_credential_retry(repo, f"pull/{pr_number}/head:{pr_ref}", origin_url)
    except ValueError as e:
        raise ValueError(f"Could not fetch PR #{pr_number} from origin: {e}") from e

    try:
        _fetch_with_credential_retry(repo, f"{base_ref_name}:{base_local_ref}", origin_url)
    except ValueError as e:
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
    allowed; only these two attack shapes -- plus a `file://` scheme, below --
    are blocked.
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
    # --repo-url exists specifically to fetch an *untrusted remote* source
    # (report/redaction code trusts a URL-shaped repo_path enough to leave it
    # unredacted -- see redact._is_url_like). A file:// URL points at the
    # local filesystem instead, so it would both defeat the "untrusted
    # remote" contract and leak a local path straight into the report the
    # same way a bare local path would (CodeRabbit review).
    if repo_url.lower().startswith("file://"):
        raise ValueError(f"Invalid repo URL {repo_url!r}: local file:// URLs are not allowed")


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

    def _attempt(extra_env: dict[str, str] | None) -> tuple[bool, str]:
        # a retried attempt needs a clean target dir -- a prior failed clone
        # may have partially written into it, and `git clone` refuses to run
        # into a non-empty directory.
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True)
        result = subprocess.run(
            [*cmd, str(tmp_dir)], capture_output=True, text=True, env=extra_env
        )
        return result.returncode == 0, result.stderr

    try:
        _try_with_credential_retry(_attempt, repo_url)
    except ValueError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise ValueError(f"Could not clone {repo_url!r}: {e}") from e

    try:
        yield tmp_dir
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
