import subprocess
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


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_git_diff_source_defaults_to_main(tmp_path: Path):
    repo = tmp_path / "codecheck"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-m", "init")

    _git(repo, "checkout", "-b", "feat/test")
    (repo / "b.py").write_text("y = 2\n")
    _git(repo, "add", "b.py")
    _git(repo, "commit", "-m", "add b")

    source = GitDiffSource(repo_path=repo)
    with source.acquire() as ws:
        assert ws.repo_path == repo.resolve()
        assert ws.mode == "diff"
        assert ws.base_ref == "main"
        assert ws.head_ref == "feat/test"
        assert ws.repo_label == "codecheck"
        assert [t.path for t in ws.targets] == ["b.py"]
