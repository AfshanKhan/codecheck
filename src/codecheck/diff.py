"""Git diff extraction: turns a repo + ref/staged selection into ReviewTarget objects."""

from __future__ import annotations

import re
from pathlib import Path

import git

from codecheck.models import ReviewTarget

_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _parse_changed_lines(diff_text: str) -> set[int]:
    """Extract line numbers touched in the new file version from a unified
    diff. A deletion-only hunk attributes to the nearest surviving new-file
    line adjacent to it, like GitHub's PR review UI does."""
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
            changed.add(max(current_line, 1))
        else:
            current_line += 1
    return changed


_GIT_QUOTE_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34}


def _unquote_git_path(raw: str) -> str:
    """Reverses git's double-quote + C-style-escape wrapping of a
    diff-header path (core.quotePath, on by default)."""
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        return raw
    inner = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(inner):
        char = inner[i]
        if char == "\\" and i + 1 < len(inner):
            nxt = inner[i + 1]
            if nxt in _GIT_QUOTE_ESCAPES:
                out.append(_GIT_QUOTE_ESCAPES[nxt])
                i += 2
                continue
            if nxt.isdigit():
                octal = inner[i + 1 : i + 4]
                try:
                    out.append(int(octal, 8))
                    i += 4
                    continue
                except ValueError:
                    pass
        out.extend(char.encode("utf-8"))
        i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return raw  # malformed escape sequence -- fall back rather than guess


def _strip_ab_prefix(path: str) -> str:
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


_QUOTED_SPEC = r'"(?:[^"\\]|\\.)*"'
# Each side of a `diff --git <old> <new>` header is quoted independently.
# Tried most-specific (both quoted) to least (neither).
_DIFF_HEADER_PATTERNS = [
    re.compile(rf"^diff --git (?P<old>{_QUOTED_SPEC}) (?P<new>{_QUOTED_SPEC})$"),
    re.compile(rf"^diff --git (?P<old>{_QUOTED_SPEC}) (?P<new>b/.+)$"),
    re.compile(rf"^diff --git (?P<old>a/.+?) (?P<new>{_QUOTED_SPEC})$"),
    re.compile(r"^diff --git (?P<old>a/.+?) (?P<new>b/.+)$"),
]


def _parse_diff_header(line: str) -> tuple[str, str] | None:
    """Splits a `diff --git <old> <new>` header line into (old_path, new_path),
    with git's per-side quoting/escaping reversed. Returns None if the line
    isn't a diff --git header at all."""
    for pattern in _DIFF_HEADER_PATTERNS:
        match = pattern.match(line)
        if match:
            old = _strip_ab_prefix(_unquote_git_path(match.group("old")))
            new = _strip_ab_prefix(_unquote_git_path(match.group("new")))
            return old, new
    return None


def _status_from_diff_item(diff_item) -> str:
    # change_type is unreliable for reversed diffs; prefer the explicit flags.
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

        header = _parse_diff_header(block.splitlines()[0])
        if header is None:
            continue
        old_path, new_path = header

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
