import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx

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
    for provider in ("groq", "mistral", "cerebras", "openrouter", "google", "openai_compatible"):
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


def test_review_calls_on_progress_per_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    targets = [
        ReviewTarget(path="a.py", status="modified", diff_text="", changed_lines={1}),
        ReviewTarget(path="b.py", status="modified", diff_text="", changed_lines={1}),
    ]

    ok_response = MagicMock()
    ok_response.raise_for_status = MagicMock()
    ok_response.json.return_value = {
        "choices": [{"message": {"tool_calls": [{
            "function": {"name": "report_findings", "arguments": json.dumps({"findings": []})}
        }]}}]
    }
    mock_client = MagicMock()
    mock_client.post.return_value = ok_response

    reviewer = OpenAICompatibleCloudReviewer(CloudConfig(enabled=True, provider="groq"), client=mock_client)
    calls = []
    reviewer.review(targets, tmp_path, on_progress=lambda path, outcome: calls.append((path, outcome)))

    assert calls == [("a.py", "0 findings"), ("b.py", "0 findings")]


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
    assert len(reviewer.skipped_files) == 1 and reviewer.skipped_files[0][0] == "a.py"
    assert reviewer.skipped_files[0][1].startswith("no report_findings tool call in response (empty reply")
    assert mock_client.post.call_count == 2                              # retried once


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
    assert len(reviewer.skipped_files) == 1 and reviewer.skipped_files[0][0] == "a.py"
    assert reviewer.skipped_files[0][1].startswith("no report_findings tool call in response (empty reply")
    assert mock_client.post.call_count == 2                              # retried once


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


def test_empty_reply_cut_off_by_length_is_retried_with_a_bigger_token_budget(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    (tmp_path / "a.py").write_text("x = 1\n")
    target = ReviewTarget(path="a.py", status="modified", diff_text="", changed_lines={1})
    cut = MagicMock()
    cut.json.return_value = {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
    ok = MagicMock()
    ok.json.return_value = {"choices": [{"message": {"tool_calls": [{"function": {"name": "report_findings", "arguments": '{"findings": []}'}}]}}]}
    mock_client = MagicMock()
    mock_client.post.side_effect = [cut, ok]
    reviewer = OpenAICompatibleCloudReviewer(CloudConfig(enabled=True, provider="google"), client=mock_client)
    assert reviewer.review([target], tmp_path) == []
    assert reviewer.skipped_files == []
    sent = [c.kwargs["json"]["max_tokens"] for c in mock_client.post.call_args_list]
    assert sent == [4096, 16384]


def test_daily_quota_429_is_not_retried_and_stops_the_remaining_files(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    for n in ("a.py", "b.py", "c.py"):
        (tmp_path / n).write_text("x = 1\n")
    targets = [ReviewTarget(path=n, status="modified", diff_text="", changed_lines={1}) for n in ("a.py", "b.py", "c.py")]
    resp = httpx.Response(429, text="Quota exceeded: GenerateRequestsPerDayPerProjectPerModel-FreeTier", request=httpx.Request("POST", "http://x"))
    mock_client = MagicMock()
    mock_client.post.return_value = resp
    reviewer = OpenAICompatibleCloudReviewer(CloudConfig(enabled=True, provider="google"), client=mock_client)
    reviewer.review(targets, tmp_path)
    assert mock_client.post.call_count == 1                              # no retries, no calls for b.py and c.py
    assert [p for p, _ in reviewer.skipped_files] == ["a.py", "b.py", "c.py"]
    assert "daily request quota" in reviewer.skipped_files[1][1]


def test_time_budget_skips_files_not_started_in_time(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    (tmp_path / "a.py").write_text("x = 1\n")
    target = ReviewTarget(path="a.py", status="modified", diff_text="", changed_lines={1})
    mock_client = MagicMock()
    reviewer = OpenAICompatibleCloudReviewer(CloudConfig(enabled=True, provider="google", time_budget_seconds=-1), client=mock_client)
    assert reviewer.review([target], tmp_path) == []
    assert reviewer.skipped_files == [("a.py", "time budget reached, not reviewed")]
    mock_client.post.assert_not_called()


def test_cloud_targets_filters_by_extension_and_the_cap_counts_only_those():
    from codecheck.reviewers.cloud_llm import cloud_targets, exceeds_audit_cap
    ts = [ReviewTarget(path=p, status="scanned", diff_text="", changed_lines=None) for p in ("a.py", "B.JS", "x.json", "y.lock", "z.md")]
    cfg = CloudConfig(extensions=[".py", ".js"], audit_file_cap=1)
    assert [t.path for t in cloud_targets(ts, cfg)] == ["a.py", "B.JS"]
    assert exceeds_audit_cap(ts, cfg, force=False) == 2
    assert len(cloud_targets(ts, CloudConfig())) == 5


def test_github_workflow_and_action_files_are_never_sent_to_an_llm():
    from codecheck.reviewers.cloud_llm import cloud_targets, exceeds_audit_cap, is_ci_path
    paths = [".github/workflows/ci.yml", "app/.github/workflows/deploy.yaml", ".github/actions/setup/action.yml", "src/app.py", "docs/github/workflows/x.py"]
    ts = [ReviewTarget(path=p, status="scanned", diff_text="", changed_lines=None) for p in paths]
    assert [is_ci_path(p) for p in paths] == [True, True, True, False, False]
    assert [t.path for t in cloud_targets(ts, CloudConfig())] == ["src/app.py", "docs/github/workflows/x.py"]      # even with no extension filter
    assert [t.path for t in cloud_targets(ts, CloudConfig(extensions=[".py", ".yml"]))] == ["src/app.py", "docs/github/workflows/x.py"]
    assert exceeds_audit_cap(ts, CloudConfig(audit_file_cap=1), force=False) == 2


def _resp(status, text, url="http://x/chat"):
    return httpx.Response(status, text=text, request=httpx.Request("POST", url))


def _review_three(tmp_path, monkeypatch, response):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    names = ("a.py", "b.py", "c.py")
    for n in names:
        (tmp_path / n).write_text("x = 1\n")
    targets = [ReviewTarget(path=n, status="modified", diff_text="", changed_lines={1}) for n in names]
    mock_client = MagicMock()
    mock_client.post.return_value = response
    reviewer = OpenAICompatibleCloudReviewer(CloudConfig(enabled=True, provider="google"), client=mock_client)
    reviewer.review(targets, tmp_path)
    return reviewer, mock_client


def test_daily_quota_is_found_even_when_the_marker_is_past_the_shortened_error_text(tmp_path: Path, monkeypatch):
    body = "Quota exceeded. " + "details " * 200 + "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
    reviewer, client = _review_three(tmp_path, monkeypatch, _resp(429, body))
    assert client.post.call_count == 1
    assert "daily request quota" in reviewer.skipped_files[1][1]


def test_a_per_day_url_does_not_stop_the_review(tmp_path: Path, monkeypatch):
    reviewer, client = _review_three(tmp_path, monkeypatch, _resp(400, "bad request", url="https://api.example.com/PerDay/chat"))
    assert client.post.call_count == 3                                                   # every file was still tried
    assert not any("daily request quota" in reason for _, reason in reviewer.skipped_files)
