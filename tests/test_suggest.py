from pathlib import Path
from unittest.mock import MagicMock

from codecheck.config import CloudConfig
from codecheck.models import Finding, ReviewTarget, Severity
from codecheck.reviewers.cloud_llm import OpenAICompatibleCloudReviewer
from codecheck.suggest import generate_suggestions


def make_reviewer(mock_client) -> OpenAICompatibleCloudReviewer:
    config = CloudConfig(
        enabled=True, provider="openai_compatible",
        base_url="http://test/v1/chat/completions", model="test-model",
    )
    return OpenAICompatibleCloudReviewer(config, client=mock_client)


def make_finding(**overrides) -> Finding:
    defaults = dict(
        check_id="RULE-001", tier="rules", source="house", severity=Severity.MEDIUM,
        title="Bare except clause", explanation="explanation", file="a.py", line_start=3,
    )
    defaults.update(overrides)
    return Finding(**defaults)


def mock_response(content: str):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.status_code = 200
    response.json.return_value = {"choices": [{"message": {"content": content}}]}
    return response


def test_generate_suggestions_fills_in_suggestion_text(tmp_path: Path):
    (tmp_path / "a.py").write_text("try:\n    pass\nexcept:\n    pass\n")
    target = ReviewTarget(path="a.py", status="modified", changed_lines={3})
    finding = make_finding()

    mock_client = MagicMock()
    mock_client.post.return_value = mock_response("Catch a specific exception, e.g. ValueError.")
    reviewer = make_reviewer(mock_client)

    count, skipped = generate_suggestions(
        reviewer, [finding], tmp_path, {"a.py": target}, max_suggestions=5, exclude_checks=set()
    )
    assert count == 1
    assert skipped == []
    assert finding.suggestion == "Catch a specific exception, e.g. ValueError."


def test_generate_suggestions_skips_finding_that_already_has_one(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")
    target = ReviewTarget(path="a.py", status="modified", changed_lines={1})
    finding = make_finding(suggestion="already has a suggestion")

    mock_client = MagicMock()
    reviewer = make_reviewer(mock_client)

    count, skipped = generate_suggestions(
        reviewer, [finding], tmp_path, {"a.py": target}, max_suggestions=5, exclude_checks=set()
    )
    assert count == 0
    mock_client.post.assert_not_called()
    assert finding.suggestion == "already has a suggestion"  # untouched


def test_generate_suggestions_respects_exclude_checks(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")
    target = ReviewTarget(path="a.py", status="modified", changed_lines={1})
    finding = make_finding(check_id="RULE-009")

    mock_client = MagicMock()
    reviewer = make_reviewer(mock_client)

    count, skipped = generate_suggestions(
        reviewer, [finding], tmp_path, {"a.py": target}, max_suggestions=5,
        exclude_checks={"RULE-009"},
    )
    assert count == 0
    mock_client.post.assert_not_called()


def test_generate_suggestions_respects_max_suggestions_cap(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")
    target = ReviewTarget(path="a.py", status="modified", changed_lines={1})
    findings = [make_finding(line_start=i) for i in range(1, 6)]  # 5 findings

    mock_client = MagicMock()
    mock_client.post.return_value = mock_response("fix it")
    reviewer = make_reviewer(mock_client)

    count, skipped = generate_suggestions(
        reviewer, findings, tmp_path, {"a.py": target}, max_suggestions=2, exclude_checks=set()
    )
    assert count == 2
    assert sum(1 for f in findings if f.suggestion is not None) == 2


def test_generate_suggestions_prioritizes_highest_severity_first(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")
    target = ReviewTarget(path="a.py", status="modified", changed_lines={1})
    low = make_finding(check_id="RULE-A", severity=Severity.LOW, line_start=1)
    high = make_finding(check_id="RULE-B", severity=Severity.HIGH, line_start=2)

    mock_client = MagicMock()
    mock_client.post.return_value = mock_response("fix it")
    reviewer = make_reviewer(mock_client)

    count, skipped = generate_suggestions(
        reviewer, [low, high], tmp_path, {"a.py": target}, max_suggestions=1, exclude_checks=set()
    )
    assert count == 1
    assert high.suggestion == "fix it"
    assert low.suggestion is None


def test_generate_suggestions_records_http_error_as_skip_not_crash(tmp_path: Path):
    import httpx

    (tmp_path / "a.py").write_text("x = 1\n")
    target = ReviewTarget(path="a.py", status="modified", changed_lines={1})
    finding = make_finding()

    mock_client = MagicMock()
    mock_client.post.side_effect = httpx.ConnectError("connection refused")
    reviewer = make_reviewer(mock_client)

    count, skipped = generate_suggestions(
        reviewer, [finding], tmp_path, {"a.py": target}, max_suggestions=5, exclude_checks=set()
    )
    assert count == 0
    assert len(skipped) == 1
    assert "suggest_fixes" in skipped[0]
    assert finding.suggestion is None


def test_generate_suggestions_finding_for_untracked_file_is_not_eligible(tmp_path: Path):
    finding = make_finding(file="not_in_targets.py")
    mock_client = MagicMock()
    reviewer = make_reviewer(mock_client)

    count, skipped = generate_suggestions(
        reviewer, [finding], tmp_path, {}, max_suggestions=5, exclude_checks=set()
    )
    assert count == 0
    mock_client.post.assert_not_called()
