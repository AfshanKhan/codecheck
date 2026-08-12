"""Whole-repo file discovery for audit mode: every git-tracked file, plus
untracked files that aren't gitignored — the set a repo owner actually
considers "their code." Each becomes a ReviewTarget with changed_lines=None,
meaning every line in the file is in scope.
"""

from __future__ import annotations

from pathlib import Path

import git

from codecheck.models import ReviewTarget


def get_repo_files(repo_path: Path) -> list[ReviewTarget]:
    repo = git.Repo(repo_path, search_parent_directories=True)

    tracked = repo.git.ls_files().splitlines()
    untracked = repo.git.ls_files(others=True, exclude_standard=True).splitlines()
    paths = sorted(set(tracked) | set(untracked))

    targets = []
    for path in paths:
        if not (repo_path / path).is_file():
            continue
        targets.append(ReviewTarget(path=path, status="scanned", diff_text="", changed_lines=None))
    return targets
