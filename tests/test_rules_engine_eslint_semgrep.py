import json
from pathlib import Path
from unittest.mock import patch

from codecheck.models import ReviewTarget
from codecheck.reviewers.rules_engine import EslintRunner, SemgrepRunner


def _fake_completed_process(stdout: str):
    class _Result:
        pass

    r = _Result()
    r.stdout = stdout
    r.returncode = 0
    return r


def test_eslint_unavailable_without_config(tmp_path: Path):
    runner = EslintRunner()
    available, reason = runner.is_available(tmp_path)
    assert available is False
    assert "config" in reason


def test_eslint_parses_and_filters_to_changed_lines(tmp_path: Path):
    (tmp_path / ".eslintrc.json").write_text("{}")
    (tmp_path / "a.js").write_text("const x = 1\nconst y = 2\n")

    changed_file = ReviewTarget(path="a.js", status="modified", diff_text="", changed_lines={1})
    eslint_output = json.dumps([
        {
            "filePath": str(tmp_path / "a.js"),
            "messages": [
                {"ruleId": "no-unused-vars", "message": "x is unused", "line": 1, "endLine": 1, "severity": 2},
                {"ruleId": "no-unused-vars", "message": "y is unused", "line": 2, "endLine": 2, "severity": 2},
            ],
        }
    ])

    with patch("codecheck.reviewers.rules_engine.shutil.which", return_value="/usr/bin/eslint"), \
         patch("codecheck.reviewers.rules_engine.subprocess.run", return_value=_fake_completed_process(eslint_output)):
        runner = EslintRunner()
        findings = runner.run([changed_file], tmp_path)

    assert len(findings) == 1
    assert findings[0].line_start == 1
    assert findings[0].check_id == "ESLINT-no-unused-vars"


def test_eslint_never_uses_repo_local_binary(tmp_path: Path):
    # regression: EslintRunner used to prefer repo_path/node_modules/.bin/eslint
    # over PATH -- auditing an untrusted repo (--repo-url/--pr) would then
    # execute a binary the repo itself shipped, i.e. arbitrary code execution.
    # It must only ever resolve eslint from PATH.
    local_bin_dir = tmp_path / "node_modules" / ".bin"
    local_bin_dir.mkdir(parents=True)
    local_bin = local_bin_dir / "eslint"
    local_bin.write_text("#!/bin/sh\necho pwned\n")
    local_bin.chmod(0o755)

    runner = EslintRunner()
    with patch("codecheck.reviewers.rules_engine.shutil.which", return_value=None):
        assert runner._binary(tmp_path) is None

    with patch("codecheck.reviewers.rules_engine.shutil.which", return_value="/usr/bin/eslint"):
        assert runner._binary(tmp_path) == "/usr/bin/eslint"


def test_eslint_and_semgrep_invocations_include_arg_separator(tmp_path: Path):
    # regression: file paths passed to these subprocess calls come from the
    # target repo's git tree with no validation -- a committed file named like
    # "--plugin=..." would otherwise be parsed as a flag instead of a path.
    (tmp_path / ".eslintrc.json").write_text("{}")
    (tmp_path / "a.js").write_text("const x = 1\n")
    changed_file = ReviewTarget(path="a.js", status="modified", diff_text="", changed_lines={1})

    with patch("codecheck.reviewers.rules_engine.shutil.which", return_value="/usr/bin/eslint"), \
         patch("codecheck.reviewers.rules_engine.subprocess.run", return_value=_fake_completed_process("[]")) as mock_run:
        EslintRunner().run([changed_file], tmp_path)
    assert "--" in mock_run.call_args.args[0]

    (tmp_path / "a.py").write_text("x = 1\n")
    changed_py = ReviewTarget(path="a.py", status="modified", diff_text="", changed_lines={1})
    with patch("codecheck.reviewers.rules_engine.shutil.which", return_value="/usr/bin/semgrep"), \
         patch("codecheck.reviewers.rules_engine.subprocess.run", return_value=_fake_completed_process("{}")) as mock_run:
        SemgrepRunner().run([changed_py], tmp_path)
    assert "--" in mock_run.call_args.args[0]
    assert "--metrics=off" in mock_run.call_args.args[0]


def test_semgrep_parses_and_filters_to_changed_lines(tmp_path: Path):
    (tmp_path / "a.py").write_text("eval(x)\nprint(1)\n")
    changed_file = ReviewTarget(path="a.py", status="modified", diff_text="", changed_lines={1})

    semgrep_output = json.dumps({
        "results": [
            {
                "check_id": "python.lang.security.audit.eval-detected",
                "path": str(tmp_path / "a.py"),
                "start": {"line": 1},
                "end": {"line": 1},
                "extra": {"message": "Detected eval", "severity": "ERROR"},
            },
            {
                "check_id": "python.lang.security.audit.eval-detected",
                "path": str(tmp_path / "a.py"),
                "start": {"line": 2},
                "end": {"line": 2},
                "extra": {"message": "Detected eval", "severity": "ERROR"},
            },
        ]
    })

    with patch("codecheck.reviewers.rules_engine.shutil.which", return_value="/usr/bin/semgrep"), \
         patch("codecheck.reviewers.rules_engine.subprocess.run", return_value=_fake_completed_process(semgrep_output)):
        runner = SemgrepRunner()
        findings = runner.run([changed_file], tmp_path)

    assert len(findings) == 1
    assert findings[0].line_start == 1
    assert findings[0].severity.value == "high"
