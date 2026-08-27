"""RULE-035: flag a committed `.env` file that isn't covered by `.gitignore`
-- `.env` files conventionally hold real secrets (API keys, DB passwords),
and one that's actually tracked in git means those secrets are in history
for anyone with repo access, not just on the machine that created the file.

A repo-wide filesystem scan, not a per-file content check -- it needs to see
the whole tree (every `.env` under repo_path, plus the repo's own
`.gitignore`) at once, which the HouseCheck interface (one file's content at
a time) has no way to provide. Implemented as its own SubRunner, the same
shape as RULE-017 (TestCoverageRunner) and RULE-019
(FrappeDbFieldCheckRunner) -- and, like RULE-019, duck-types the SubRunner
interface (is_available/run/name) instead of importing the ABC from
rules_engine.py, to avoid a circular import (that module imports this one to
wire the runner in).

Doesn't try to detect secrets *inside* file content beyond this -- RULE-009/
RULE-016 (hardcoded_credential.py / js_hardcoded_credential.py) already flag
a hardcoded-looking secret literal wherever it appears in reviewed source;
this specifically covers the "wrong file committed at all" case those can't,
since an untracked-by-git-history .env file's *content* is never something
those per-file checks would see in a diff.
"""

from __future__ import annotations

from pathlib import Path

import pathspec

from codecheck.models import Finding, ReviewTarget, Severity


def _read_gitignore_patterns(repo_path: Path) -> list[str]:
    gitignore = repo_path / ".gitignore"
    if not gitignore.is_file():
        return []
    try:
        return gitignore.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def _is_ignored(rel_path: str, patterns: list[str]) -> bool:
    """Real gitignore semantics (wildcards, directory anchoring, negation --
    '!path' un-ignoring a file another pattern covers) via `pathspec`'s
    'gitwildmatch' matcher, rather than a hand-rolled approximation.

    A prior hand-rolled version only handled a few fixed shapes by comparing
    the pattern string itself against a short list of candidates -- it
    couldn't recognize a negation pattern at all, so a `.gitignore` with a
    broad `.env` ignore *and* a narrower `!committed/.env` un-ignore for one
    deliberately-tracked file would still treat that file as covered and
    silently miss it, the exact false negative this check exists to catch
    (caught by Graphite AI review on the PR that introduced this check).
    """
    spec = pathspec.PathSpec.from_lines("gitignore", patterns)
    return spec.match_file(rel_path)


class SecretsInRepoRunner:
    check_id = "RULE-035"
    title = "Committed .env file not covered by .gitignore"
    severity = Severity.HIGH
    name = "secrets_in_repo"

    def is_available(self, repo_path: Path) -> tuple[bool, str | None]:
        return True, None

    def run(self, targets: list[ReviewTarget], repo_path: Path) -> list[Finding]:
        if not repo_path.is_dir():
            return []
        patterns = _read_gitignore_patterns(repo_path)
        findings = []
        for env_path in sorted(repo_path.rglob(".env")):
            if not env_path.is_file():
                continue
            rel_path = env_path.relative_to(repo_path).as_posix()
            if _is_ignored(rel_path, patterns):
                continue
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
