"""Regression tests for the content-field fallback: some OpenAI-compatible
servers (observed with Ollama's qwen2.5-coder:7b) put the tool call's JSON in
`message.content` instead of `message.tool_calls`, even with tool_choice=
"required". This is a strict json.loads, not regex-scraping of prose.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

from codecheck.config import LocalConfig
from codecheck.models import ReviewTarget
from codecheck.reviewers.local_llm import LocalLLMReviewer
from codecheck.reviewers.openai_protocol import _extract_findings_from_content


def test_extract_findings_from_content_with_findings_key_directly():
    content = json.dumps({"findings": [{"severity": "high", "title": "x", "explanation": "y", "line_start": 1}]})
    result = _extract_findings_from_content(content)
    assert result == [{"severity": "high", "title": "x", "explanation": "y", "line_start": 1}]


def test_extract_findings_from_content_with_name_and_arguments_dict():
    content = json.dumps(
        {"name": "report_findings", "arguments": {"findings": [{"severity": "low", "title": "z", "explanation": "w", "line_start": 2}]}}
    )
    result = _extract_findings_from_content(content)
    assert result == [{"severity": "low", "title": "z", "explanation": "w", "line_start": 2}]


def test_extract_findings_from_content_with_arguments_as_json_string():
    content = json.dumps(
        {"name": "report_findings", "arguments": json.dumps({"findings": [{"severity": "medium", "title": "a", "explanation": "b", "line_start": 3}]})}
    )
    result = _extract_findings_from_content(content)
    assert result == [{"severity": "medium", "title": "a", "explanation": "b", "line_start": 3}]


def test_extract_findings_from_content_returns_none_for_genuinely_malformed_json():
    # this is the actual malformed output observed from a real Ollama request:
    # an unquoted bareword value, which is not valid JSON
    content = '{\n  "name": report_findings,\n  "arguments": {"findings": []}\n}'
    assert _extract_findings_from_content(content) is None


def test_extract_findings_from_content_returns_none_for_plain_prose():
    assert _extract_findings_from_content("I looked at the file and it seems fine.") is None


def test_extract_findings_from_content_returns_none_for_empty_or_missing():
    assert _extract_findings_from_content(None) is None
    assert _extract_findings_from_content("") is None


def test_extract_findings_from_content_returns_none_for_wrong_function_name():
    content = json.dumps({"name": "something_else", "arguments": {"findings": []}})
    assert _extract_findings_from_content(content) is None


def test_extract_findings_from_content_returns_none_for_non_object_finding_element():
    # same bug class as the tool_calls path: a findings array containing a
    # non-object element (e.g. a bare string) must not be handed back as if
    # it were usable -- the caller would crash on raw.get(...) downstream.
    content = json.dumps({"findings": ["not-a-dict"]})
    assert _extract_findings_from_content(content) is None

    content = json.dumps({"name": "report_findings", "arguments": {"findings": [123]}})
    assert _extract_findings_from_content(content) is None


def test_review_file_recovers_via_content_fallback_when_tool_calls_empty(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")
    target = ReviewTarget(path="a.py", status="modified", diff_text="", changed_lines={1})

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "name": "report_findings",
                            "arguments": {
                                "findings": [
                                    {"severity": "high", "title": "Recovered", "explanation": "e", "line_start": 1}
                                ]
                            },
                        }
                    )
                }
            }
        ]
    }
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    reviewer = LocalLLMReviewer(
        LocalConfig(enabled=True, provider="ollama", model="qwen2.5-coder:7b"), client=mock_client
    )
    findings = reviewer.review([target], tmp_path)

    assert len(findings) == 1
    assert findings[0].title == "Recovered"
    assert reviewer.skipped_files == []
