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


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init(repo: Path) -> None:
    repo.mkdir(exist_ok=True)
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test")


def test_get_repo_files_keeps_non_ascii_filenames(tmp_path: Path):
    # git quotes non-ASCII names in plain ls-files output ("caf\\303\\251.py"); they were dropped
    repo = tmp_path / "r"
    _init(repo)
    (repo / "café.py").write_text("x = 1\n")
    (repo / "ok.py").write_text("y = 1\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "i")

    assert sorted(t.path for t in get_repo_files(repo)) == ["café.py", "ok.py"]


def test_get_repo_files_on_a_subdirectory_returns_paths_relative_to_it(tmp_path: Path):
    # paths used to be repo-root-relative, so none resolved under repo_path and nothing was audited
    repo = tmp_path / "r"
    _init(repo)
    (repo / "top.py").write_text("x = 1\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "s.py").write_text("y = 1\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "i")

    assert [t.path for t in get_repo_files(repo / "sub")] == ["s.py"]


def test_get_repo_files_outside_a_git_repo_raises_value_error(tmp_path: Path):
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    with pytest.raises(ValueError, match="not a readable git repository"):
        get_repo_files(plain)
