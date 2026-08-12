import subprocess
from pathlib import Path

import pytest

from codecheck.repo_scan import get_repo_files


@pytest.fixture
def sandbox_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sandbox"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")

    (repo / "tracked.py").write_text("x = 1\n")
    (repo / ".gitignore").write_text("ignored.py\nbuild/\n")
    run("add", "-A")
    run("commit", "-q", "-m", "initial")

    (repo / "untracked.py").write_text("y = 2\n")
    (repo / "ignored.py").write_text("z = 3\n")
    (repo / "build").mkdir()
    (repo / "build" / "artifact.py").write_text("w = 4\n")

    return repo


def test_includes_tracked_and_untracked_not_ignored(sandbox_repo: Path):
    targets = get_repo_files(sandbox_repo)
    paths = {t.path for t in targets}

    assert "tracked.py" in paths
    assert "untracked.py" in paths
    assert ".gitignore" in paths


def test_excludes_gitignored_files(sandbox_repo: Path):
    targets = get_repo_files(sandbox_repo)
    paths = {t.path for t in targets}

    assert "ignored.py" not in paths
    assert "build/artifact.py" not in paths


def test_targets_have_no_line_filter(sandbox_repo: Path):
    targets = get_repo_files(sandbox_repo)
    tracked = next(t for t in targets if t.path == "tracked.py")

    assert tracked.changed_lines is None
    assert tracked.status == "scanned"
