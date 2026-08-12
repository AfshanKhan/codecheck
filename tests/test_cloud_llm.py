from pathlib import Path
from unittest.mock import MagicMock

from codecheck.config import CloudConfig
from codecheck.models import ReviewTarget
from codecheck.reviewers.cloud_llm import AnthropicCloudReviewer, exceeds_audit_cap


def test_is_available_requires_enabled_and_api_key(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    reviewer = AnthropicCloudReviewer(CloudConfig(enabled=False))
    available, reason = reviewer.is_available(tmp_path)
    assert available is False
    assert "disabled" in reason

    reviewer = AnthropicCloudReviewer(CloudConfig(enabled=True))
    available, reason = reviewer.is_available(tmp_path)
    assert available is False
    assert "ANTHROPIC_API_KEY" in reason

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    available, reason = reviewer.is_available(tmp_path)
    assert available is True


def test_review_parses_tool_use_findings(tmp_path: Path):
    (tmp_path / "a.py").write_text("def foo():\n    eval(user_input)\n")
    changed_file = ReviewTarget(
        path="a.py", status="modified", diff_text="@@ -1,1 +1,2 @@\n+    eval(user_input)",
        changed_lines={2},
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "content": [
            {
                "type": "tool_use",
                "name": "report_findings",
                "input": {
                    "findings": [
                        {
                            "severity": "critical",
                            "title": "Use of eval() on user input",
                            "explanation": "Arbitrary code execution risk.",
                            "line_start": 2,
                            "line_end": 2,
                        }
                    ]
                },
            }
        ]
    }
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    reviewer = AnthropicCloudReviewer(CloudConfig(enabled=True), client=mock_client)
    findings = reviewer.review([changed_file], tmp_path)

    assert len(findings) == 1
    assert findings[0].check_id == "CLOUD-001"
    assert findings[0].severity.value == "critical"
    assert findings[0].file == "a.py"
    assert reviewer.skipped_files == []

    # verify request shape: forced tool choice, file content + diff included
    call_kwargs = mock_client.post.call_args
    payload = call_kwargs.kwargs["json"]
    assert payload["tool_choice"] == {"type": "tool", "name": "report_findings"}
    assert "eval(user_input)" in payload["messages"][0]["content"]


def test_review_skips_oversized_files(tmp_path: Path):
    big_content = "\n".join(f"x = {i}" for i in range(50))
    (tmp_path / "big.py").write_text(big_content)
    changed_file = ReviewTarget(path="big.py", status="modified", diff_text="", changed_lines={1})

    mock_client = MagicMock()
    reviewer = AnthropicCloudReviewer(CloudConfig(enabled=True, max_file_lines=10), client=mock_client)
    findings = reviewer.review([changed_file], tmp_path)

    assert findings == []
    assert reviewer.skipped_files == [("big.py", "file too large (50 lines > 10)")]
    mock_client.post.assert_not_called()


def test_review_audit_mode_no_diff_omits_diff_block(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")
    scanned_target = ReviewTarget(path="a.py", status="scanned", diff_text="", changed_lines=None)

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "content": [{"type": "tool_use", "name": "report_findings", "input": {"findings": []}}]
    }
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    reviewer = AnthropicCloudReviewer(CloudConfig(enabled=True), client=mock_client)
    reviewer.review([scanned_target], tmp_path)

    payload = mock_client.post.call_args.kwargs["json"]
    message = payload["messages"][0]["content"]
    assert "full-file audit" in message
    assert "Unified diff" not in message


def test_exceeds_audit_cap_blocks_over_limit_without_force(tmp_path: Path):
    targets = [ReviewTarget(path=f"f{i}.py", status="scanned") for i in range(5)]
    config = CloudConfig(enabled=True, audit_file_cap=3)

    assert exceeds_audit_cap(targets, config, force=False) == 5
    assert exceeds_audit_cap(targets, config, force=True) is None


def test_exceeds_audit_cap_allows_under_limit(tmp_path: Path):
    targets = [ReviewTarget(path=f"f{i}.py", status="scanned") for i in range(2)]
    config = CloudConfig(enabled=True, audit_file_cap=3)

    assert exceeds_audit_cap(targets, config, force=False) is None


def test_exceeds_audit_cap_ignores_deleted_files(tmp_path: Path):
    targets = [ReviewTarget(path=f"f{i}.py", status="deleted") for i in range(10)]
    config = CloudConfig(enabled=True, audit_file_cap=3)

    assert exceeds_audit_cap(targets, config, force=False) is None


def test_review_records_api_error_as_skip(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")
    changed_file = ReviewTarget(path="a.py", status="modified", diff_text="", changed_lines={1})

    import httpx as httpx_module

    mock_client = MagicMock()
    mock_client.post.side_effect = httpx_module.ConnectError("connection refused")

    reviewer = AnthropicCloudReviewer(CloudConfig(enabled=True), client=mock_client)
    findings = reviewer.review([changed_file], tmp_path)

    assert findings == []
    assert len(reviewer.skipped_files) == 1
    assert reviewer.skipped_files[0][0] == "a.py"
