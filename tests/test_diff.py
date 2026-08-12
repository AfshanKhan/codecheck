import subprocess
from pathlib import Path

import pytest

from codecheck.diff import get_diff, read_file_content
from codecheck.models import ReviewTarget


def test_invalid_base_ref_raises_value_error_not_git_command_error(sandbox_repo: Path):
    # regression: an invalid --base-ref used to raise git.GitCommandError
    # straight out of get_diff, which cli.py doesn't catch, crashing the CLI
    # with a raw traceback instead of a clean "Error: ..." message + exit 2.
    with pytest.raises(ValueError, match="Invalid base_ref"):
        get_diff(sandbox_repo, base_ref="this-branch-does-not-exist", staged=False)


@pytest.fixture
def sandbox_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sandbox"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")

    (repo / "a.py").write_text("def foo():\n    return 1\n")
    run("add", "a.py")
    run("commit", "-q", "-m", "initial")
    run("branch", "-m", "main")

    run("checkout", "-q", "-b", "feature")
    (repo / "a.py").write_text("def foo():\n    return 2\n\n\ndef bar():\n    pass\n")
    (repo / "b.py").write_text("x = 1\n")
    run("add", "-A")
    run("commit", "-q", "-m", "feature change")

    return repo


def test_base_ref_diff_finds_changed_files(sandbox_repo: Path):
    changed = get_diff(sandbox_repo, base_ref="main", staged=False)
    paths = {c.path for c in changed}
    assert paths == {"a.py", "b.py"}

    a = next(c for c in changed if c.path == "a.py")
    assert a.status == "modified"
    assert 2 in a.changed_lines  # "return 2" line

    b = next(c for c in changed if c.path == "b.py")
    assert b.status == "added"
    assert 1 in b.changed_lines


def test_staged_diff(sandbox_repo: Path):
    (sandbox_repo / "a.py").write_text("def foo():\n    return 3\n")
    subprocess.run(["git", "add", "a.py"], cwd=sandbox_repo, check=True, capture_output=True)

    changed = get_diff(sandbox_repo, base_ref=None, staged=True)
    assert len(changed) == 1
    assert changed[0].path == "a.py"
    assert changed[0].status == "modified"
    assert 2 in changed[0].changed_lines


def test_staged_new_file_reports_added_status(sandbox_repo: Path):
    (sandbox_repo / "c.py").write_text("y = 2\n")
    subprocess.run(["git", "add", "c.py"], cwd=sandbox_repo, check=True, capture_output=True)

    changed = get_diff(sandbox_repo, base_ref=None, staged=True)
    assert len(changed) == 1
    assert changed[0].path == "c.py"
    assert changed[0].status == "added"


def test_read_file_content(sandbox_repo: Path):
    changed = get_diff(sandbox_repo, base_ref="main", staged=False)
    b = next(c for c in changed if c.path == "b.py")
    content = read_file_content(sandbox_repo, b)
    assert content == "x = 1\n"


def test_read_file_content_refuses_path_that_escapes_repo(tmp_path: Path, sandbox_repo: Path):
    # regression: target.path is git-derived and normally repo-relative, but
    # there was no guard against a path like "../../etc/passwd" reaching outside
    # repo_path -- since file content is shipped to a cloud LLM, this needs to
    # fail closed rather than trust the path is well-formed.
    secret = tmp_path / "secret.txt"
    secret.write_text("do not leak this\n")

    target = ReviewTarget(path="../secret.txt", status="modified", diff_text="")
    content = read_file_content(sandbox_repo, target)
    assert content is None
