import json
from pathlib import Path
from unittest.mock import MagicMock

from codecheck.config import CloudConfig
from codecheck.models import ReviewTarget
from codecheck.reviewers.cloud_llm import (
    AnthropicCloudReviewer,
    OpenAICompatibleCloudReviewer,
    build_cloud_reviewer,
)


def test_build_cloud_reviewer_picks_anthropic_for_anthropic_provider():
    reviewer = build_cloud_reviewer(CloudConfig(provider="anthropic"))
    assert isinstance(reviewer, AnthropicCloudReviewer)


def test_build_cloud_reviewer_picks_openai_compatible_for_free_providers():
    for provider in ("groq", "mistral", "cerebras", "openrouter", "openai_compatible"):
        reviewer = build_cloud_reviewer(CloudConfig(provider=provider))
        assert isinstance(reviewer, OpenAICompatibleCloudReviewer)


def test_named_provider_resolves_base_url_and_api_key_env_from_preset(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    reviewer = OpenAICompatibleCloudReviewer(CloudConfig(enabled=True, provider="groq"))

    available, reason = reviewer.is_available(tmp_path)
    assert available is False
    assert "GROQ_API_KEY" in reason

    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    available, reason = reviewer.is_available(tmp_path)
    assert available is True
    assert reviewer._resolved_base_url() == "https://api.groq.com/openai/v1/chat/completions"


def test_openai_compatible_provider_requires_explicit_base_url(tmp_path: Path):
    reviewer = OpenAICompatibleCloudReviewer(CloudConfig(enabled=True, provider="openai_compatible"))
    available, reason = reviewer.is_available(tmp_path)
    assert available is False
    assert "base_url" in reason


def test_openai_compatible_custom_base_url_with_no_key_needed(tmp_path: Path):
    # e.g. a local LM Studio server with no auth at all
    reviewer = OpenAICompatibleCloudReviewer(
        CloudConfig(enabled=True, provider="openai_compatible", base_url="http://localhost:1234/v1/chat/completions", api_key_env="")
    )
    available, reason = reviewer.is_available(tmp_path)
    assert available is True, reason


def test_arbitrary_provider_name_with_explicit_base_url_works_without_a_preset(tmp_path: Path, monkeypatch):
    # self-hosted vLLM (or any other self-hosted OpenAI-compatible server) has
    # no single canonical URL the way Groq/Mistral/Cerebras/OpenRouter do, so
    # there's deliberately no dedicated preset for it -- any provider string
    # works as long as base_url is set explicitly, since presets are only
    # consulted when base_url is absent.
    reviewer = OpenAICompatibleCloudReviewer(
        CloudConfig(
            enabled=True,
            provider="vllm",
            base_url="https://my-vllm-deployment.example.com/v1/chat/completions",
            api_key_env="VLLM_API_KEY",
        )
    )
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    available, reason = reviewer.is_available(tmp_path)
    assert available is False
    assert "VLLM_API_KEY" in reason

    monkeypatch.setenv("VLLM_API_KEY", "sk-test")
    available, reason = reviewer.is_available(tmp_path)
    assert available is True
    assert reviewer._resolved_base_url() == "https://my-vllm-deployment.example.com/v1/chat/completions"


def test_review_parses_openai_style_tool_call(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    (tmp_path / "a.py").write_text("def foo():\n    eval(user_input)\n")
    target = ReviewTarget(
        path="a.py", status="modified", diff_text="@@ -1,1 +1,2 @@\n+    eval(user_input)",
        changed_lines={2},
    )

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
                                            {
                                                "severity": "critical",
                                                "title": "Use of eval() on user input",
                                                "explanation": "Arbitrary code execution risk.",
                                                "line_start": 2,
                                                "line_end": 2,
                                            }
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

    reviewer = OpenAICompatibleCloudReviewer(
        CloudConfig(enabled=True, provider="groq"), client=mock_client
    )
    findings = reviewer.review([target], tmp_path)

    assert len(findings) == 1
    assert findings[0].check_id == "CLOUD-001"
    assert findings[0].severity.value == "critical"
    assert reviewer.skipped_files == []

    call_kwargs = mock_client.post.call_args
    assert call_kwargs.args[0] == "https://api.groq.com/openai/v1/chat/completions"
    payload = call_kwargs.kwargs["json"]
    assert payload["tool_choice"] == "required"
    assert "eval(user_input)" in payload["messages"][1]["content"]


def test_review_handles_missing_tool_call_as_skip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    (tmp_path / "a.py").write_text("x = 1\n")
    target = ReviewTarget(path="a.py", status="modified", diff_text="", changed_lines={1})

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"choices": [{"message": {}}]}
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    reviewer = OpenAICompatibleCloudReviewer(
        CloudConfig(enabled=True, provider="groq"), client=mock_client
    )
    findings = reviewer.review([target], tmp_path)

    assert findings == []
    assert reviewer.skipped_files == [("a.py", "no report_findings tool call in response")]


def test_review_handles_non_json_response_body_as_skip_not_crash(tmp_path: Path, monkeypatch):
    # regression: a provider returning HTTP 200 with a non-JSON body (e.g. an
    # HTML error page from a proxy/gateway in front of the real API) used to
    # raise an uncaught json.JSONDecodeError out of response.json(), crashing
    # the whole CLI instead of skipping just that file.
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    (tmp_path / "a.py").write_text("x = 1\n")
    target = ReviewTarget(path="a.py", status="modified", diff_text="", changed_lines={1})

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.side_effect = json.JSONDecodeError("bad", "<html>not json</html>", 0)
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    reviewer = OpenAICompatibleCloudReviewer(CloudConfig(enabled=True, provider="groq"), client=mock_client)
    findings = reviewer.review([target], tmp_path)

    assert findings == []
    assert reviewer.skipped_files == [("a.py", "response was not valid JSON")]


def test_review_handles_non_object_json_response_as_skip_not_crash(tmp_path: Path, monkeypatch):
    # regression: valid JSON that isn't an object (e.g. a bare JSON array or
    # string) used to raise TypeError out of data["choices"], uncaught.
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    (tmp_path / "a.py").write_text("x = 1\n")
    target = ReviewTarget(path="a.py", status="modified", diff_text="", changed_lines={1})

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = ["unexpected", "array"]
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    reviewer = OpenAICompatibleCloudReviewer(CloudConfig(enabled=True, provider="groq"), client=mock_client)
    findings = reviewer.review([target], tmp_path)

    assert findings == []
    assert reviewer.skipped_files == [("a.py", "response JSON was not an object")]


def test_review_handles_malformed_tool_call_shapes_as_skip_not_crash(tmp_path: Path, monkeypatch):
    # regression: a non-dict entry in tool_calls, or a non-dict "function",
    # used to raise AttributeError (calling .get on a non-dict) uncaught.
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    (tmp_path / "a.py").write_text("x = 1\n")
    target = ReviewTarget(path="a.py", status="modified", diff_text="", changed_lines={1})

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"tool_calls": ["not-a-dict", {"function": "also-not-a-dict"}]}}]
    }
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    reviewer = OpenAICompatibleCloudReviewer(CloudConfig(enabled=True, provider="groq"), client=mock_client)
    findings = reviewer.review([target], tmp_path)

    assert findings == []
    assert reviewer.skipped_files == [("a.py", "no report_findings tool call in response")]


def test_review_handles_non_object_finding_element_as_skip_not_crash(tmp_path: Path, monkeypatch):
    # regression: findings was validated as a list, but not that each element
    # is an object -- a string/number element reached _finding_from_raw's
    # raw.get(...) uncaught. Same bug class caught in the Anthropic parser by
    # a second round of Greptile review; this is the shared OpenAI-compatible
    # path (used by both the cloud tier and the local LLM tier).
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    (tmp_path / "a.py").write_text("x = 1\n")
    target = ReviewTarget(path="a.py", status="modified", diff_text="", changed_lines={1})

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
                                "arguments": json.dumps({"findings": ["not-a-dict-finding"]}),
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

    assert findings == []
    assert reviewer.skipped_files == [("a.py", "tool call 'findings' contained a non-object element")]


def test_get_client_sends_bearer_auth_header(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-secret")
    reviewer = OpenAICompatibleCloudReviewer(CloudConfig(enabled=True, provider="groq"))
    client = reviewer._get_client()
    assert client.headers["authorization"] == "Bearer gsk-secret"
