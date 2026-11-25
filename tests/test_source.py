from pathlib import Path

from codecheck.models import ReviewTarget
from codecheck.source import GitDiffSource, InMemorySource, compute_repo_label


def test_in_memory_source():
    targets = [ReviewTarget(path="a.py", status="added", diff_text="+hello", changed_lines={1})]
    source = InMemorySource(targets=targets, repo_path=Path("/tmp/fake"), repo_label="test_fake")
    with source.acquire() as ws:
        assert ws.repo_path == Path("/tmp/fake")
        assert len(ws.targets) == 1
        assert ws.targets[0].path == "a.py"
        assert ws.repo_label == "test_fake"
        assert ws.mode == "audit"


def test_compute_repo_label():
    assert compute_repo_label(Path("/var/code/my-repo")) == "my-repo"
    assert compute_repo_label(Path("/tmp"), "https://github.com/foo/bar.git") == "foo_bar"
    assert compute_repo_label(Path("/tmp"), "git@github.com:foo/baz.git") == "foo_baz"


def test_git_diff_source_on_current_repo():
    source = GitDiffSource(repo_path=Path.cwd())
    with source.acquire() as ws:
        assert ws.repo_path == Path.cwd().resolve()
        assert ws.mode == "diff"
        assert ws.repo_label == "codecheck"
        assert isinstance(ws.targets, list)
