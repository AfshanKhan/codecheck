import subprocess
from pathlib import Path

import pytest

from codecheck.diff import (
    _parse_changed_lines,
    _parse_diff_header,
    _unquote_git_path,
    get_diff,
    read_file_content,
)
from codecheck.models import ReviewTarget


def test_invalid_base_ref_raises_value_error_not_git_command_error(sandbox_repo: Path):
    # regression: an invalid --base-ref used to raise git.GitCommandError
    # straight out of get_diff, which cli.py doesn't catch, crashing the CLI
    # with a raw traceback instead of a clean "Error: ..." message + exit 2.
    with pytest.raises(ValueError, match="Invalid base_ref"):
        get_diff(sandbox_repo, base_ref="this-branch-does-not-exist", staged=False)


def test_parse_changed_lines_pure_addition():
    diff_text = "@@ -1,2 +1,3 @@\n a\n+b\n c\n"
    assert _parse_changed_lines(diff_text) == {2}


def test_parse_changed_lines_deletion_only_attributes_to_adjacent_surviving_line():
    # regression (Greptile): a hunk that only removes lines (no + lines at
    # all) used to leave changed_lines completely empty for that hunk, making
    # a deletion-only diff invisible to every line-scoped rule -- e.g. RULE-018
    # never firing on a function that's still too long after some lines were
    # deleted from it, just because nothing was *added*. Deletions now
    # attribute to the nearest surviving new-file line adjacent to them.
    diff_text = "@@ -1,4 +1,2 @@\n a\n-b\n-c\n d\n"
    # new file is just "a\nd\n" -- line 1 (a) unchanged, line 2 (d) is where
    # the deleted "b"/"c" lines used to sit.
    assert _parse_changed_lines(diff_text) == {2}


def test_parse_changed_lines_deletion_at_start_of_hunk():
    diff_text = "@@ -1,3 +1,1 @@\n-a\n-b\n c\n"
    assert _parse_changed_lines(diff_text) == {1}


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


def test_unquote_git_path_decodes_octal_escaped_utf8():
    # git wraps a diff-header path in double quotes with octal-escaped bytes
    # for non-ASCII filenames when core.quotePath is on (the default) --
    # "café.py" becomes "caf\303\251.py" in the raw header.
    assert _unquote_git_path('"caf\\303\\251.py"') == "café.py"


def test_unquote_git_path_decodes_literal_quote_and_backslash():
    assert _unquote_git_path('"weird\\"name\\\\.py"') == 'weird"name\\.py'


def test_unquote_git_path_leaves_unquoted_paths_alone():
    assert _unquote_git_path("plain/path.py") == "plain/path.py"


@pytest.mark.parametrize(
    "header,expected",
    [
        # both sides quoted (non-ASCII filename)
        (
            'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"',
            ("café.py", "café.py"),
        ),
        # neither side quoted, old path contains a space -- the pre-existing
        # non-greedy-up-to-" b/" heuristic must keep working
        ("diff --git a/with space.py b/plain.py", ("with space.py", "plain.py")),
        # rename FROM plain TO non-ASCII: only the new side is quoted
        (
            'diff --git a/plain.py "b/renam\\303\\251.py"',
            ("plain.py", "renamé.py"),
        ),
        # rename FROM non-ASCII TO plain: only the old side is quoted
        (
            'diff --git "a/renam\\303\\251.py" b/back_to_plain.py',
            ("renamé.py", "back_to_plain.py"),
        ),
    ],
)
def test_parse_diff_header_handles_every_quoting_combination(header: str, expected: tuple[str, str]):
    # regression: the original single regex (`diff --git a/(.+?) b/(.+)`)
    # simply failed to match -- silently dropping the file from the diff --
    # whenever git quoted the header, which it does by default for any
    # non-ASCII filename (core.quotePath) or a literal quote/backslash in the
    # name. Confirmed via Greptile review and reproduced directly against
    # real `git diff` output for all four quoting combinations above.
    assert _parse_diff_header(header) == expected


def test_parse_diff_header_returns_none_for_non_header_line():
    assert _parse_diff_header("not a diff header") is None


def test_base_ref_diff_finds_quoted_unicode_filename(tmp_path: Path):
    # end-to-end: get_diff() must actually surface a changed file with a
    # non-ASCII name, not just parse its header correctly in isolation.
    repo = tmp_path / "unicode_repo"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    run("config", "core.quotepath", "true")  # explicit -- this is also git's default

    (repo / "a.py").write_text("x = 1\n")
    run("add", "a.py")
    run("commit", "-q", "-m", "initial")
    run("branch", "-m", "main")

    run("checkout", "-q", "-b", "feature")
    (repo / "café.py").write_text("y = 2\n")
    run("add", "café.py")
    run("commit", "-q", "-m", "add unicode file")

    changed = get_diff(repo, base_ref="main", staged=False)
    paths = {c.path for c in changed}
    assert "café.py" in paths
    assert not any("\\3" in p for p in paths)  # no leftover octal-escaped junk


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
