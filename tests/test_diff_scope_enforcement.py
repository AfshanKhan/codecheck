"""Regression tests: confirmed against a real Groq request that the model can
report a finding on a line that wasn't actually part of the diff, despite the
system prompt asking it not to. within_diff_scope() enforces this
programmatically for both OpenAI-compatible and Anthropic backends.
"""

from pathlib import Path
from unittest.mock import MagicMock

from codecheck.config import CloudConfig
from codecheck.models import Finding, ReviewTarget, Severity
from codecheck.reviewers.cloud_llm import AnthropicCloudReviewer
from codecheck.reviewers.openai_protocol import within_diff_scope


def _finding(line_start: int, line_end: int | None = None) -> Finding:
    return Finding(
        check_id="X-001", tier="cloud_llm", source="cloud_llm", severity=Severity.HIGH,
        title="t", explanation="e", file="f.py", line_start=line_start, line_end=line_end,
    )


def test_audit_mode_no_scoping_everything_in_scope():
    target = ReviewTarget(path="f.py", status="scanned", changed_lines=None)
    assert within_diff_scope(target, _finding(999)) is True


def test_diff_mode_exact_changed_line_in_scope():
    target = ReviewTarget(path="f.py", status="modified", changed_lines={5})
    assert within_diff_scope(target, _finding(5)) is True


def test_diff_mode_within_tolerance_window_in_scope():
    target = ReviewTarget(path="f.py", status="modified", changed_lines={5})
    assert within_diff_scope(target, _finding(6)) is True
    assert within_diff_scope(target, _finding(3)) is True


def test_diff_mode_far_from_changed_lines_out_of_scope():
    # the real case observed: diff touched line 8, model reported line 4
    target = ReviewTarget(path="f.py", status="modified", changed_lines={8})
    assert within_diff_scope(target, _finding(4)) is False


def test_diff_mode_line_range_overlapping_changed_lines_in_scope():
    target = ReviewTarget(path="f.py", status="modified", changed_lines={10})
    assert within_diff_scope(target, _finding(line_start=8, line_end=12)) is True


def test_anthropic_reviewer_filters_out_of_scope_findings(tmp_path: Path):
    (tmp_path / "f.py").write_text("\n".join(f"line{i}" for i in range(1, 15)) + "\n")
    target = ReviewTarget(path="f.py", status="modified", diff_text="diff", changed_lines={8})

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "content": [
            {
                "type": "tool_use",
                "name": "report_findings",
                "input": {
                    "findings": [
                        {"severity": "high", "title": "in scope", "explanation": "e", "line_start": 8},
                        {"severity": "high", "title": "out of scope", "explanation": "e", "line_start": 1},
                    ]
                },
            }
        ]
    }
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    reviewer = AnthropicCloudReviewer(CloudConfig(enabled=True), client=mock_client)
    findings = reviewer.review([target], tmp_path)

    assert len(findings) == 1
    assert findings[0].title == "in scope"


def test_openai_compatible_reviewer_filters_out_of_scope_findings(tmp_path: Path):
    import json

    from codecheck.reviewers.cloud_llm import OpenAICompatibleCloudReviewer

    (tmp_path / "f.py").write_text("\n".join(f"line{i}" for i in range(1, 15)) + "\n")
    target = ReviewTarget(path="f.py", status="modified", diff_text="diff", changed_lines={8})

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "report_findings",
                                "arguments": json.dumps(
                                    {
                                        "findings": [
                                            {"severity": "high", "title": "in scope", "explanation": "e", "line_start": 8},
                                            {"severity": "high", "title": "out of scope", "explanation": "e", "line_start": 1},
                                        ]
                                    }
                                ),
                            }
                        }
                    ]
                }
            }
        ]
    }
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    reviewer = OpenAICompatibleCloudReviewer(CloudConfig(enabled=True, provider="groq"), client=mock_client)
    findings = reviewer.review([target], tmp_path)

    assert len(findings) == 1
    assert findings[0].title == "in scope"
