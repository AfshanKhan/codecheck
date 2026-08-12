import json
from pathlib import Path
from unittest.mock import MagicMock

from codecheck.config import LocalConfig
from codecheck.models import ReviewTarget
from codecheck.reviewers.local_llm import LocalLLMReviewer


def test_is_available_requires_enabled_base_url_and_model(tmp_path: Path):
    reviewer = LocalLLMReviewer(LocalConfig(enabled=False))
    available, reason = reviewer.is_available(tmp_path)
    assert available is False
    assert "disabled" in reason

    reviewer = LocalLLMReviewer(
        LocalConfig(enabled=True, provider="openai_compatible", base_url="", model="qwen2.5-coder")
    )
    available, reason = reviewer.is_available(tmp_path)
    assert available is False
    assert "base_url" in reason

    reviewer = LocalLLMReviewer(LocalConfig(enabled=True, model=""))
    available, reason = reviewer.is_available(tmp_path)
    assert available is False
    assert "model" in reason

    reviewer = LocalLLMReviewer(LocalConfig(enabled=True, model="qwen2.5-coder"))
    available, reason = reviewer.is_available(tmp_path)
    assert available is True


def test_provider_presets_resolve_expected_base_urls(tmp_path: Path):
    lm_studio = LocalLLMReviewer(LocalConfig(enabled=True, provider="lm_studio", model="m"))
    assert lm_studio._resolved_base_url() == "http://localhost:1234/v1/chat/completions"

    ollama = LocalLLMReviewer(LocalConfig(enabled=True, provider="ollama", model="m"))
    assert ollama._resolved_base_url() == "http://localhost:11434/v1/chat/completions"

    custom = LocalLLMReviewer(
        LocalConfig(enabled=True, provider="ollama", base_url="http://elsewhere:9999/v1/chat/completions", model="m")
    )
    assert custom._resolved_base_url() == "http://elsewhere:9999/v1/chat/completions"


def test_is_available_respects_optional_api_key_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MY_LOCAL_KEY", raising=False)
    reviewer = LocalLLMReviewer(
        LocalConfig(enabled=True, model="qwen2.5-coder", api_key_env="MY_LOCAL_KEY")
    )
    available, reason = reviewer.is_available(tmp_path)
    assert available is False
    assert "MY_LOCAL_KEY" in reason

    monkeypatch.setenv("MY_LOCAL_KEY", "secret")
    available, _ = reviewer.is_available(tmp_path)
    assert available is True


def test_review_parses_tool_call_against_default_lm_studio_url(tmp_path: Path):
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
                                                "severity": "high",
                                                "title": "Use of eval() on user input",
                                                "explanation": "Arbitrary code execution risk.",
                                                "line_start": 2,
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

    reviewer = LocalLLMReviewer(
        LocalConfig(enabled=True, model="qwen2.5-coder"), client=mock_client
    )
    findings = reviewer.review([target], tmp_path)

    assert len(findings) == 1
    assert findings[0].check_id == "LOCAL-001"
    assert findings[0].tier == "local_llm"
    assert findings[0].source == "local_llm"
    assert reviewer.skipped_files == []

    call_kwargs = mock_client.post.call_args
    assert call_kwargs.args[0] == "http://localhost:1234/v1/chat/completions"


def test_request_sets_max_tokens(tmp_path: Path):
    # regression: llama-server (llama.cpp) has no sane default max_tokens --
    # a real request against it generated 6000+ tokens without ever calling
    # the tool, blowing past the client timeout. Every OpenAI-compatible
    # request must cap this explicitly rather than trust the server.
    (tmp_path / "a.py").write_text("x = 1\n")
    target = ReviewTarget(path="a.py", status="modified", diff_text="", changed_lines={1})

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"choices": [{"message": {"tool_calls": []}}]}
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    reviewer = LocalLLMReviewer(
        LocalConfig(enabled=True, provider="openai_compatible", base_url="http://localhost:8080/v1/chat/completions", model="m"),
        client=mock_client,
    )
    reviewer.review([target], tmp_path)

    payload = mock_client.post.call_args.kwargs["json"]
    assert "max_tokens" in payload
    assert payload["max_tokens"] > 0


def test_review_skips_oversized_files(tmp_path: Path):
    big_content = "\n".join(f"x = {i}" for i in range(10))
    (tmp_path / "big.py").write_text(big_content)
    target = ReviewTarget(path="big.py", status="modified", diff_text="", changed_lines={1})

    mock_client = MagicMock()
    reviewer = LocalLLMReviewer(
        LocalConfig(enabled=True, model="qwen2.5-coder", max_file_lines=5), client=mock_client
    )
    findings = reviewer.review([target], tmp_path)

    assert findings == []
    assert reviewer.skipped_files == [("big.py", "file too large (10 lines > 5)")]
    mock_client.post.assert_not_called()


def test_get_client_has_no_auth_header_when_no_api_key_env(tmp_path: Path):
    reviewer = LocalLLMReviewer(LocalConfig(enabled=True, model="qwen2.5-coder"))
    client = reviewer._get_client()
    assert "authorization" not in client.headers


def test_get_client_uses_local_default_timeout_of_300s(tmp_path: Path):
    # regression: a real llama-server on CPU legitimately needed minutes for
    # one request; the shared default (120s, fine for hosted APIs) was too
    # short. LocalConfig's default must be higher than CloudConfig's.
    reviewer = LocalLLMReviewer(LocalConfig(enabled=True, model="qwen2.5-coder"))
    client = reviewer._get_client()
    assert client.timeout.read == 300.0


def test_get_client_respects_custom_timeout(tmp_path: Path):
    reviewer = LocalLLMReviewer(
        LocalConfig(enabled=True, model="qwen2.5-coder", request_timeout_seconds=600.0)
    )
    client = reviewer._get_client()
    assert client.timeout.read == 600.0
