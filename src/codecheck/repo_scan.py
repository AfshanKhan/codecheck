"""Whole-repo file discovery for audit mode: every git-tracked file, plus
untracked files that aren't gitignored. Each becomes a ReviewTarget with
changed_lines=None."""

from __future__ import annotations

from pathlib import Path

import git

from codecheck.models import ReviewTarget


def get_repo_files(repo_path: Path) -> list[ReviewTarget]:
    # Run git from repo_path itself, so the listed paths are relative to it (the frame
    # every runner and read_file_content use) even when it's a subdirectory of the repo.
    # -z keeps non-ASCII names verbatim; without it git quotes them ("caf\303\251.py")
    # and the is_file() check below silently drops the file.
    git_in_path = git.Git(str(repo_path))
    tracked = git_in_path.ls_files(z=True).split("\0")
    untracked = git_in_path.ls_files(z=True, others=True, exclude_standard=True).split("\0")
    paths = sorted((set(tracked) | set(untracked)) - {""})

    targets = []
    for path in paths:
        if not (repo_path / path).is_file():
            continue
        targets.append(ReviewTarget(path=path, status="scanned", diff_text="", changed_lines=None))
    return targets
