"""Git diff extraction: turns a repo + ref/staged selection into ReviewTarget objects."""

from __future__ import annotations

import re
from pathlib import Path

import git

from codecheck.models import ReviewTarget

_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _parse_changed_lines(diff_text: str) -> set[int]:
    """Extract line numbers added/modified in the new file version from a unified diff."""
    changed: set[int] = set()
    current_line = 0
    for raw_line in diff_text.splitlines():
        match = _HUNK_HEADER_RE.match(raw_line)
        if match:
            current_line = int(match.group(1))
            continue
        if current_line == 0:
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            changed.add(current_line)
            current_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            continue  # removed line, doesn't advance new-file line counter
        else:
            current_line += 1
    return changed


def _status_from_diff_item(diff_item) -> str:
    # change_type is unreliable for reversed diffs (e.g. staged new files show
    # change_type=None), so prefer the explicit new_file/deleted_file/renamed_file flags.
    if diff_item.renamed_file:
        return "renamed"
    if diff_item.new_file:
        return "added"
    if diff_item.deleted_file:
        return "deleted"
    return "modified"


def get_diff(repo_path: Path, base_ref: str | None, staged: bool) -> list[ReviewTarget]:
    """Return changed files for either staged changes or a base_ref...HEAD diff.

    For base_ref, diffs against the merge-base with HEAD (three-dot semantics)
    so unrelated commits on base_ref since the branch forked aren't included.
    """
    repo = git.Repo(repo_path, search_parent_directories=True)

    if staged:
        diff_index = repo.index.diff("HEAD", create_patch=True, R=True)
    else:
        if not base_ref:
            raise ValueError("base_ref is required when staged=False")
        try:
            merge_base_results = repo.merge_base(base_ref, "HEAD")
        except git.GitCommandError as e:
            raise ValueError(f"Invalid base_ref {base_ref!r}: {e}") from e
        if not merge_base_results:
            raise ValueError(f"No merge base found between {base_ref!r} and HEAD")
        merge_base = merge_base_results[0]
        diff_index = repo.git.diff(merge_base.hexsha, "HEAD", patch=True)
        return _parse_raw_diff(diff_index)

    return _diff_index_to_targets(diff_index)


def _diff_index_to_targets(diff_index) -> list[ReviewTarget]:
    targets = []
    for diff_item in diff_index:
        path = diff_item.b_path or diff_item.a_path
        diff_text = diff_item.diff.decode("utf-8", errors="replace") if diff_item.diff else ""
        targets.append(
            ReviewTarget(
                path=path,
                status=_status_from_diff_item(diff_item),
                diff_text=diff_text,
                changed_lines=_parse_changed_lines(diff_text),
                old_path=diff_item.a_path if diff_item.renamed_file else None,
            )
        )
    return targets


def _parse_raw_diff(raw_diff_text: str) -> list[ReviewTarget]:
    """Split `git diff` plain text output into per-file ReviewTarget entries."""
    if not raw_diff_text.strip():
        return []

    file_blocks = re.split(r"(?=^diff --git )", raw_diff_text, flags=re.MULTILINE)
    targets = []
    for block in file_blocks:
        if not block.startswith("diff --git"):
            continue

        header_match = re.match(r"diff --git a/(.+?) b/(.+)", block.splitlines()[0])
        if not header_match:
            continue
        old_path, new_path = header_match.group(1), header_match.group(2)

        if "new file mode" in block:
            status = "added"
        elif "deleted file mode" in block:
            status = "deleted"
        elif "rename from" in block:
            status = "renamed"
        else:
            status = "modified"

        targets.append(
            ReviewTarget(
                path=new_path,
                status=status,
                diff_text=block,
                changed_lines=_parse_changed_lines(block),
                old_path=old_path if status == "renamed" else None,
            )
        )
    return targets


def read_file_content(repo_path: Path, target: ReviewTarget) -> str | None:
    """Read the current (working-tree) content of a target file, for LLM context."""
    if target.status == "deleted":
        return None
    resolved_repo_path = Path(repo_path).resolve()
    full_path = (resolved_repo_path / target.path).resolve()
    if not full_path.is_relative_to(resolved_repo_path):
        return None  # target.path escaped repo_path (e.g. "../../etc/passwd")
    if not full_path.is_file():
        return None
    try:
        return full_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
