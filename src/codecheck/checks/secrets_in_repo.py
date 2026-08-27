"""RULE-035: flag an `.env` file present in the checkout that isn't covered
by any `.gitignore` -- `.env` files conventionally hold real secrets (API
keys, DB passwords). This is deliberately a presence check, not a
`git ls-files`-backed tracked-status check: catching an `.env` before
`.gitignore` covers it (whether or not it's been committed yet) is at least
as valuable as catching one after the fact, and avoids depending on git
state (a bare working tree, no `.git` at all when scanning a plain
directory) that a filesystem scan doesn't need. The explanation text
reflects this -- "if it's tracked by git," not an assertion that it is.

A repo-wide filesystem scan, not a per-file content check -- it needs to see
the whole tree (every `.env` under repo_path, plus every `.gitignore` that
could cover it) at once, which the HouseCheck interface (one file's content
at a time) has no way to provide. Implemented as its own SubRunner, the same
shape as RULE-017 (TestCoverageRunner) and RULE-019
(FrappeDbFieldCheckRunner) -- and, like RULE-019, duck-types the SubRunner
interface (is_available/run/name) instead of importing the ABC from
rules_engine.py, to avoid a circular import (that module imports this one to
wire the runner in).

Doesn't try to detect secrets *inside* file content beyond this -- RULE-009/
RULE-016 (hardcoded_credential.py / js_hardcoded_credential.py) already flag
a hardcoded-looking secret literal wherever it appears in reviewed source;
this specifically covers the "wrong file present at all" case those can't,
since an ignored (or not-yet-committed) `.env` file's *content* is never
something those per-file checks would see in a diff.
"""

from __future__ import annotations

from pathlib import Path

import pathspec

from codecheck.models import Finding, ReviewTarget, Severity


def _read_gitignore_patterns(gitignore_path: Path) -> list[str]:
    if not gitignore_path.is_file():
        return []
    try:
        return gitignore_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def _is_ignored(repo_path: Path, env_path: Path) -> bool:
    """Real gitignore semantics (wildcards, directory anchoring, negation --
    '!path' un-ignoring a file another pattern covers) via `pathspec`'s
    'gitignore' matcher, rather than a hand-rolled approximation.

    A prior hand-rolled version only handled a few fixed shapes by comparing
    the pattern string itself against a short list of candidates -- it
    couldn't recognize a negation pattern at all, so a `.gitignore` with a
    broad `.env` ignore *and* a narrower `!committed/.env` un-ignore for one
    deliberately-tracked file would still treat that file as covered and
    silently miss it, the exact false negative this check exists to catch
    (caught by Graphite AI review on the PR that introduced this check).

    Checks every directory from `repo_path` down to `env_path`'s own
    directory for a `.gitignore`, not just the repo root -- a real Frappe
    app commonly has its own `.gitignore` a few levels down from the repo
    root that a root-only check would simply never see (caught by CodeRabbit
    review on the same PR). Each `.gitignore` found is matched against the
    `.env` path *relative to that `.gitignore`'s own directory* -- the same
    frame of reference git itself uses -- not the repo root, since a nested
    `.gitignore`'s patterns are scoped to its own subtree. Any directory's
    `.gitignore` matching is enough to call the file covered -- doesn't
    implement git's full closest-.gitignore-wins precedence for a negation
    pattern that disagrees *across* levels (e.g. a root `.gitignore`
    ignoring `.env` and a *nested* one un-ignoring it back for one specific
    file) -- deliberately out of scope: that specific cross-level override
    is rare, and treating "any covering pattern, anywhere" as sufficient is
    the safe direction for a check whose job is not missing a real secret.
    """
    current = env_path.parent
    while True:
        patterns = _read_gitignore_patterns(current / ".gitignore")
        if patterns:
            rel_to_here = env_path.relative_to(current).as_posix()
            if pathspec.PathSpec.from_lines("gitignore", patterns).match_file(rel_to_here):
                return True
        if current == repo_path:
            break
        current = current.parent
    return False


class SecretsInRepoRunner:
    check_id = "RULE-035"
    title = ".env file present and not covered by .gitignore"
    severity = Severity.HIGH
    name = "secrets_in_repo"

    def is_available(self, repo_path: Path) -> tuple[bool, str | None]:
        return True, None

    def run(self, targets: list[ReviewTarget], repo_path: Path) -> list[Finding]:
        if not repo_path.is_dir():
            return []
        findings = []
        for env_path in sorted(repo_path.rglob(".env")):
            if not env_path.is_file():
                continue
            if _is_ignored(repo_path, env_path):
                continue
            rel_path = env_path.relative_to(repo_path).as_posix()
            findings.append(
                Finding(
                    check_id=self.check_id,
                    tier="rules",
                    source="house",
                    severity=self.severity,
                    title=self.title,
                    explanation=(
                        f"'{rel_path}' is present in this checkout and not matched by any "
                        ".gitignore pattern. If it's tracked by git, any secret inside it "
                        "(API keys, DB passwords) is in repo history for anyone with access, "
                        "not just this machine -- add it to .gitignore and rotate any secret "
                        "it contains if it's already been committed."
                    ),
                    file=rel_path,
                    line_start=1,
                    line_end=1,
                )
            )
        return findings
