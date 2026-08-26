"""Tier 1: rules engine. Wraps ruff/eslint/semgrep/house-checks as sub-runners and
normalizes their output into our Finding schema, filtered to lines touched by the
diff (or unfiltered, in whole-repo audit mode where changed_lines is None).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from codecheck.checks.registry import ALL_CHECKS, load_extra_checks
from codecheck.config import RulesConfig
from codecheck.diff import read_file_content
from codecheck.models import Finding, ReviewTarget, Severity
from codecheck.reviewers.base import Reviewer


def _line_in_scope(target: ReviewTarget, line: int) -> bool:
    return target.changed_lines is None or line in target.changed_lines


class SubRunner(ABC):
    name: str

    @abstractmethod
    def is_available(self, repo_path: Path) -> tuple[bool, str | None]:
        ...

    @abstractmethod
    def run(self, targets: list[ReviewTarget], repo_path: Path) -> list[Finding]:
        ...


def _filter_targets(targets: list[ReviewTarget], suffixes: tuple[str, ...]) -> list[ReviewTarget]:
    return [t for t in targets if t.status != "deleted" and t.path.endswith(suffixes)]


class RuffRunner(SubRunner):
    name = "ruff"

    def is_available(self, repo_path: Path) -> tuple[bool, str | None]:
        if shutil.which("ruff") is None:
            return False, "ruff not found on PATH"
        return True, None

    def run(self, targets: list[ReviewTarget], repo_path: Path) -> list[Finding]:
        py_targets = _filter_targets(targets, (".py",))
        if not py_targets:
            return []

        target_by_path = {t.path: t for t in py_targets}
        result = subprocess.run(
            ["ruff", "check", "--output-format=json", "--", *target_by_path.keys()],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        # ruff exits 1 when it finds lint errors — that's expected, not a failure.
        if result.returncode not in (0, 1):
            return []

        try:
            raw_findings = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return []

        findings = []
        for item in raw_findings:
            path = item.get("filename", "")
            rel_path = _relativize(path, repo_path)
            target = target_by_path.get(rel_path)
            if target is None:
                continue
            line = item["location"]["row"]
            if not _line_in_scope(target, line):
                continue
            findings.append(
                Finding(
                    check_id=f"RUFF-{item['code']}",
                    tier="rules",
                    source="ruff",
                    severity=_ruff_severity(item["code"]),
                    title=item["code"] + ": " + item["message"].split("\n")[0],
                    explanation=item["message"],
                    file=rel_path,
                    line_start=line,
                    line_end=item.get("end_location", {}).get("row"),
                    raw=item,
                )
            )
        return findings


def _relativize(path: str, repo_path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(repo_path.resolve()))
    except ValueError:
        return path


def _ruff_severity(code: str) -> Severity:
    # Security-relevant rule families get bumped; everything else is style/correctness.
    if code.startswith("S"):  # flake8-bandit security rules
        return Severity.HIGH
    if code.startswith(("E9", "F82")):  # syntax errors, undefined names
        return Severity.HIGH
    if code.startswith(("F", "B")):  # pyflakes, bugbear correctness
        return Severity.MEDIUM
    return Severity.LOW


_ESLINT_CONFIG_NAMES = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
)


class EslintRunner(SubRunner):
    name = "eslint"

    def _binary(self, repo_path: Path) -> str | None:
        # Deliberately PATH-only, never repo_path/node_modules/.bin/eslint: when
        # auditing an untrusted repo (--repo-url, --pr), that binary is shipped
        # by whoever wrote the repo, not by the user running codecheck -- running
        # it would be arbitrary code execution under the attacker's control.
        return shutil.which("eslint")

    def is_available(self, repo_path: Path) -> tuple[bool, str | None]:
        if not any((repo_path / name).is_file() for name in _ESLINT_CONFIG_NAMES):
            return False, "no eslint config found in repo"
        if self._binary(repo_path) is None:
            return False, "eslint binary not found on PATH"
        return True, None

    def run(self, targets: list[ReviewTarget], repo_path: Path) -> list[Finding]:
        js_targets = _filter_targets(targets, (".js", ".jsx", ".ts", ".tsx"))
        if not js_targets:
            return []

        target_by_path = {t.path: t for t in js_targets}
        binary = self._binary(repo_path)
        result = subprocess.run(
            [binary, "--format=json", "--", *target_by_path.keys()],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        try:
            raw_results = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return []

        findings = []
        for file_result in raw_results:
            rel_path = _relativize(file_result.get("filePath", ""), repo_path)
            target = target_by_path.get(rel_path)
            if target is None:
                continue
            for msg in file_result.get("messages", []):
                line = msg.get("line")
                if line is None or not _line_in_scope(target, line):
                    continue
                rule_id = msg.get("ruleId") or "unknown"
                findings.append(
                    Finding(
                        check_id=f"ESLINT-{rule_id}",
                        tier="rules",
                        source="eslint",
                        severity=Severity.MEDIUM if msg.get("severity") == 2 else Severity.LOW,
                        title=f"{rule_id}: {msg.get('message', '')}",
                        explanation=msg.get("message", ""),
                        file=rel_path,
                        line_start=line,
                        line_end=msg.get("endLine"),
                        raw=msg,
                    )
                )
        return findings


_SEMGREP_SEVERITY_MAP = {
    "ERROR": Severity.HIGH,
    "WARNING": Severity.MEDIUM,
    "INFO": Severity.LOW,
}


class SemgrepRunner(SubRunner):
    name = "semgrep"

    def is_available(self, repo_path: Path) -> tuple[bool, str | None]:
        if shutil.which("semgrep") is None:
            return False, "semgrep not found on PATH"
        return True, None

    def run(self, targets: list[ReviewTarget], repo_path: Path) -> list[Finding]:
        live_targets = [t for t in targets if t.status != "deleted"]
        if not live_targets:
            return []

        target_by_path = {t.path: t for t in live_targets}
        result = subprocess.run(
            # --metrics=off: --config=auto necessarily reaches semgrep's registry
            # to download rules (unavoidable if you want its ruleset), but it
            # also sends anonymous scan telemetry by default -- confirmed to be
            # a separate, disable-able behavior. Turned off since this tool
            # documents itself as not sending your code's contents anywhere
            # without you opting in, and telemetry isn't something users opted
            # into here.
            ["semgrep", "--config=auto", "--metrics=off", "--json", "--quiet", "--", *target_by_path.keys()],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return []

        findings = []
        for item in payload.get("results", []):
            rel_path = _relativize(item.get("path", ""), repo_path)
            target = target_by_path.get(rel_path)
            if target is None:
                continue
            line = item.get("start", {}).get("line")
            if line is None or not _line_in_scope(target, line):
                continue
            extra = item.get("extra", {})
            check_id = item.get("check_id", "semgrep-rule").split(".")[-1]
            findings.append(
                Finding(
                    check_id=f"SEMGREP-{check_id}",
                    tier="rules",
                    source="semgrep",
                    severity=_SEMGREP_SEVERITY_MAP.get(extra.get("severity", "WARNING"), Severity.MEDIUM),
                    title=extra.get("message", check_id).split("\n")[0],
                    explanation=extra.get("message", ""),
                    file=rel_path,
                    line_start=line,
                    line_end=item.get("end", {}).get("line"),
                    raw=item,
                )
            )
        return findings


class HouseRulesRunner(SubRunner):
    name = "house_rules"

    def __init__(self, extra_checks: list | None = None):
        # Project-supplied checks (rules.extra_checks in config.yaml) run
        # alongside the built-ins, sharing the exact same per-file loop below
        # -- a HouseCheck doesn't know or care whether it's built-in or
        # loaded from config, they're interchangeable.
        self._checks = [*ALL_CHECKS, *(extra_checks or [])]

    def is_available(self, repo_path: Path) -> tuple[bool, str | None]:
        return True, None

    def run(self, targets: list[ReviewTarget], repo_path: Path) -> list[Finding]:
        # .py for the AST-based checks, .js for the regex/line-based Frappe
        # client-script checks -- each HouseCheck filters by its own extension.
        py_targets = _filter_targets(targets, (".py", ".js"))
        findings: list[Finding] = []
        for target in py_targets:
            content = read_file_content(repo_path, target)
            if content is None:
                continue
            for check in self._checks:
                findings.extend(check.check_file(target.path, content, target.changed_lines))
        return findings


_APP_EXTENSIONS = (".py", ".js", ".jsx", ".ts", ".tsx")
_TEST_MARKERS = ("def test_", "test(", "it(", "describe(")

# A plain `"test" in path` substring match also fires on ordinary application
# filenames like contest.tsx or latest.ts -- match test-file *conventions*
# instead: a bare test.py / test_*.py / *_test.py module, a bare test.<ext> /
# .test./.spec. / *_test.<ext> JS/TS file, or a path inside a
# tests/__tests__ directory. (The bare "test.ext" and JS/TS "*_test.ext" forms
# were added after Greptile caught the first version rejecting them --
# test.py, test.tsx, and foo_test.js all being real, common test filenames.)
_PY_TEST_PATH_RE = re.compile(r"(^|/)(test(_[^/]+)?|[^/]+_test)\.py$")
_JS_TEST_PATH_RE = re.compile(r"(^|/)(test|[^/]+\.(test|spec)|[^/]+_test)\.(js|jsx|ts|tsx)$")
_TEST_DIR_RE = re.compile(r"(^|/)(tests?|__tests__)/")


def _is_test_path(path: str) -> bool:
    lowered = path.lower()
    if not lowered.endswith(_APP_EXTENSIONS):
        return False
    return bool(
        _PY_TEST_PATH_RE.search(lowered)
        or _JS_TEST_PATH_RE.search(lowered)
        or _TEST_DIR_RE.search(lowered)
    )


def _added_line_count(diff_text: str) -> int:
    return sum(
        1 for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++")
    )


def _looks_like_real_test(target: ReviewTarget) -> bool:
    """Mirrors pr_probe's PRAnalyzer.check_tests(): a test file only counts if
    its diff has an actual test declaration, or -- when there's no patch to
    inspect, or the patch is boilerplate (e.g. a stub `pass` body) -- a large
    enough addition count or a real modification to give it the benefit of the
    doubt.
    """
    if any(marker in target.diff_text for marker in _TEST_MARKERS):
        return True
    additions = _added_line_count(target.diff_text)
    if "pass" in target.diff_text and additions < 15:
        return False  # boilerplate stub, not a real test
    if additions > 15:
        return True
    return target.status == "modified" and additions > 0


class TestCoverageRunner(SubRunner):
    """Flags a diff that changes non-trivial application code but touches no
    test file -- ported from pr_probe's PRAnalyzer.check_tests() heuristic
    (a PR-metrics tool, not a code-review tool, but the same signal is a
    useful house-rule-style finding here). Diff-only: in audit mode every
    target has changed_lines=None and there's no single "this change" to
    judge against, so it's a no-op there.
    """

    name = "test_coverage"

    def is_available(self, repo_path: Path) -> tuple[bool, str | None]:
        return True, None

    def run(self, targets: list[ReviewTarget], repo_path: Path) -> list[Finding]:
        if not targets or all(t.changed_lines is None for t in targets):
            return []

        app_targets = [
            t
            for t in targets
            if t.status != "deleted" and t.path.endswith(_APP_EXTENSIONS) and not _is_test_path(t.path)
        ]
        if not app_targets:
            return []

        total_app_additions = sum(_added_line_count(t.diff_text) for t in app_targets)
        if total_app_additions < 5:
            return []  # too small a change to reasonably expect test coverage

        test_targets = [t for t in targets if t.status != "deleted" and _is_test_path(t.path)]
        if any(_looks_like_real_test(t) for t in test_targets):
            return []

        anchor = max(app_targets, key=lambda t: _added_line_count(t.diff_text))
        return [
            Finding(
                check_id="RULE-017",
                tier="rules",
                source="house",
                severity=Severity.LOW,
                title="No test changes detected for this PR",
                explanation=(
                    f"This change adds/modifies {total_app_additions} line(s) of application "
                    "code across "
                    f"{len(app_targets)} file(s), but no file in the diff looks like a test "
                    "(a path containing 'test' with an actual test declaration). If this change "
                    "has behavior worth covering, consider adding or updating a test."
                ),
                file=anchor.path,
                line_start=1,
                line_end=1,
            )
        ]


class RulesEngineReviewer(Reviewer):
    tier = "rules"
    name = "rules_engine"

    def __init__(self, config: RulesConfig):
        self.config = config
        # Sub-runners that were enabled but couldn't run (tool not installed, no
        # eslint config, etc.), as (runner_name, reason) pairs. Populated by
        # review(); surfaced by the CLI so an enabled-but-skipped linter isn't
        # silently mistaken for "ran and found nothing."
        self.skipped_runners: list[tuple[str, str]] = []
        # A misconfigured entry in rules.extra_checks (bad path, import error,
        # not a HouseCheck) is recorded here at construction time rather than
        # raised -- review() folds these into skipped_runners on the first
        # call, same visibility as any other skipped sub-runner, without
        # taking the built-in checks down with it.
        extra_checks, extra_check_errors = load_extra_checks(config.extra_checks)
        self._extra_check_errors = extra_check_errors
        self._runners: list[SubRunner] = []
        if config.ruff:
            self._runners.append(RuffRunner())
        if config.eslint:
            self._runners.append(EslintRunner())
        if config.semgrep:
            self._runners.append(SemgrepRunner())
        if config.house_rules:
            self._runners.append(HouseRulesRunner(extra_checks=extra_checks))
        if config.test_coverage:
            self._runners.append(TestCoverageRunner())

    def is_available(self, repo_path: Path) -> tuple[bool, str | None]:
        if not self.config.enabled or not self._runners:
            return False, "rules engine disabled or no sub-runners enabled"
        return True, None

    def review(self, targets: list[ReviewTarget], repo_path: Path) -> list[Finding]:
        self.skipped_runners = [
            ("house_rules.extra_checks", error) for error in self._extra_check_errors
        ]
        findings: list[Finding] = []
        for runner in self._runners:
            available, reason = runner.is_available(repo_path)
            if not available:
                if reason:
                    self.skipped_runners.append((runner.name, reason))
                continue
            findings.extend(runner.run(targets, repo_path))
        if self.config.disabled_checks:
            disabled = set(self.config.disabled_checks)
            findings = [f for f in findings if f.check_id not in disabled]
        return findings
