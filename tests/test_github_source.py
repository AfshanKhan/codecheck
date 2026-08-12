"""Exercises pr_worktree/cloned_repo against a local bare repo standing in for
'origin' — no real network calls, no GitHub involved. A local bare repo behaves
identically to a GitHub remote for git's own fetch/clone protocol, which is all
this module actually depends on.
"""

import subprocess
from pathlib import Path

import pytest

from codecheck.diff import get_diff
from codecheck.github_source import _validate_clone_url, cloned_repo, parse_pr_url, pr_worktree


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def fake_origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Returns (bare_origin_path, local_clone_path). The clone has a 'main'
    branch, plus a PR-like ref (refs/pull/5/head) on the bare origin pointing at
    a commit not on main — simulating an open GitHub PR #5.
    """
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "remote", "add", "origin", str(bare))

    (work / "a.py").write_text("x = 1\n")
    _git(work, "add", "a.py")
    _git(work, "commit", "-q", "-m", "initial")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-q", "origin", "main")
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")

    _git(work, "checkout", "-q", "-b", "feature")
    (work / "a.py").write_text("x = 2\n")
    _git(work, "add", "a.py")
    _git(work, "commit", "-q", "-m", "pr change")
    pr_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "push", "-q", "origin", "feature")
    _git(work, "checkout", "-q", "main")

    # simulate the PR ref GitHub exposes for every open PR
    subprocess.run(
        ["git", "update-ref", "refs/pull/5/head", pr_sha], cwd=bare, check=True, capture_output=True
    )

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(bare), str(clone))
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")

    return bare, clone


def test_pr_worktree_fetches_pr_and_diffs_against_base(fake_origin_and_clone):
    _bare, clone = fake_origin_and_clone

    with pr_worktree(clone, 5, base_ref_override="main") as (worktree_path, base_ref):
        assert worktree_path.is_dir()
        targets = get_diff(worktree_path, base_ref=base_ref, staged=False)
        assert len(targets) == 1
        assert targets[0].path == "a.py"
        assert 1 in targets[0].changed_lines


def test_pr_worktree_cleans_up_on_exit(fake_origin_and_clone):
    _bare, clone = fake_origin_and_clone

    with pr_worktree(clone, 5, base_ref_override="main") as (worktree_path, _base_ref):
        captured_path = worktree_path

    assert not captured_path.exists()
    # temp refs should be gone too
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/codecheck/pr-5"],
        cwd=clone,
    )
    assert result.returncode != 0


def test_pr_worktree_unknown_pr_raises_value_error(fake_origin_and_clone):
    _bare, clone = fake_origin_and_clone

    with pytest.raises(ValueError, match="Could not fetch PR"):
        with pr_worktree(clone, 999, base_ref_override="main"):
            pass


def test_cloned_repo_clones_and_cleans_up(fake_origin_and_clone):
    bare, _clone = fake_origin_and_clone

    with cloned_repo(str(bare)) as cloned_path:
        assert cloned_path.is_dir()
        assert (cloned_path / "a.py").is_file()
        captured_path = cloned_path

    assert not captured_path.exists()


def test_cloned_repo_bad_url_raises_value_error(tmp_path: Path):
    with pytest.raises(ValueError, match="Could not clone"):
        with cloned_repo(str(tmp_path / "does-not-exist")):
            pass


def test_cloned_repo_with_branch_checks_out_that_branch(fake_origin_and_clone):
    bare, _clone = fake_origin_and_clone

    with cloned_repo(str(bare), branch="feature") as cloned_path:
        result = subprocess.run(
            ["git", "branch", "--show-current"], cwd=cloned_path, capture_output=True, text=True
        )
        assert result.stdout.strip() == "feature"
        content = (cloned_path / "a.py").read_text()
        assert content == "x = 2\n"  # the feature-branch commit, not main's


def test_cloned_repo_rejects_branch_starting_with_dash(fake_origin_and_clone):
    # same argument-injection shape as repo_url, just for the --branch value.
    bare, _clone = fake_origin_and_clone
    with pytest.raises(ValueError, match="Invalid branch"):
        with cloned_repo(str(bare), branch="--upload-pack=touch /tmp/pwned"):
            pass


@pytest.mark.parametrize(
    "malicious_url",
    [
        "-oProxyCommand=touch /tmp/pwned",
        "--upload-pack=touch /tmp/pwned",
        "ext::sh -c touch% /tmp/pwned",
        "fd::0",
    ],
)
def test_cloned_repo_rejects_argument_injection_and_transport_helper_urls(malicious_url: str):
    # regression: repo_url reached `git clone <repo_url> <dir>` with no
    # validation -- a leading '-' is argument injection into git clone, and
    # git's '::' transport-helper syntax (ext::, fd::) can execute arbitrary
    # commands. Both must be rejected before ever calling subprocess.
    with pytest.raises(ValueError, match="Invalid repo URL"):
        with cloned_repo(malicious_url):
            pass


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/org/repo/pull/123", ("https://github.com/org/repo", 123)),
        ("https://github.com/org/repo/pull/123/files", ("https://github.com/org/repo", 123)),
        ("https://github.com/org/repo/pull/123/", ("https://github.com/org/repo", 123)),
        ("https://github.com/org/repo.git/pull/123", ("https://github.com/org/repo", 123)),
        # GitHub Enterprise / any host that follows the same /pull/<n> shape
        ("https://git.example.com/org/repo/pull/9", ("https://git.example.com/org/repo", 9)),
    ],
)
def test_parse_pr_url_extracts_repo_and_number(url: str, expected: tuple[str, int]):
    assert parse_pr_url(url) == expected


@pytest.mark.parametrize(
    "not_a_pr_url",
    [
        "123",
        "https://github.com/org/repo",
        "https://github.com/org/repo/issues/123",
        "git@github.com:org/repo.git",
        "",
    ],
)
def test_parse_pr_url_returns_none_for_non_pr_urls(not_a_pr_url: str):
    assert parse_pr_url(not_a_pr_url) is None


def test_validate_clone_url_allows_ordinary_urls_and_local_paths():
    for ok_url in (
        "https://github.com/org/repo.git",
        "git@github.com:org/repo.git",
        "ssh://git@github.com/org/repo.git",
        "/tmp/some/local/bare/repo.git",
        # IPv6 literal: contains '::' but only inside the authority (after '//'),
        # so it is NOT the ext::/fd:: transport-helper shape and must be allowed.
        "https://[::1]:443/org/repo.git",
    ):
        _validate_clone_url(ok_url)  # must not raise
