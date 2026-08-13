from unittest.mock import MagicMock, patch

import httpx
import pytest

from codecheck.reviewers.openai_protocol import post_with_retry


def _response(status_code: int, headers: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    if status_code >= 400:
        request = httpx.Request("POST", "https://example.test/v1/chat/completions")
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"{status_code} error", request=request, response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def test_post_with_retry_succeeds_immediately_on_200():
    client = MagicMock()
    client.post.return_value = _response(200)

    result = post_with_retry(client, "https://example.test", {})

    assert client.post.call_count == 1
    assert result.status_code == 200


def test_post_with_retry_retries_on_429_then_succeeds():
    client = MagicMock()
    client.post.side_effect = [_response(429, {"retry-after": "1"}), _response(200)]

    with patch("codecheck.reviewers.openai_protocol.time.sleep") as mock_sleep:
        result = post_with_retry(client, "https://example.test", {})

    assert client.post.call_count == 2
    assert result.status_code == 200
    mock_sleep.assert_called_once_with(1.0)  # honored the Retry-After header exactly


def test_post_with_retry_caps_an_excessive_retry_after_value():
    # regression: a malicious or misconfigured server returning a huge
    # Retry-After (e.g. 999999s) was passed straight to time.sleep uncapped
    # -- only the fallback backoff had _MAX_RETRY_DELAY_SECONDS applied.
    client = MagicMock()
    client.post.side_effect = [_response(429, {"retry-after": "999999"}), _response(200)]

    with patch("codecheck.reviewers.openai_protocol.time.sleep") as mock_sleep:
        post_with_retry(client, "https://example.test", {})

    mock_sleep.assert_called_once_with(60.0)  # capped, not the raw 999999


def test_post_with_retry_falls_back_to_exponential_backoff_without_retry_after_header():
    client = MagicMock()
    client.post.side_effect = [_response(429), _response(429), _response(200)]

    with patch("codecheck.reviewers.openai_protocol.time.sleep") as mock_sleep:
        result = post_with_retry(client, "https://example.test", {})

    assert result.status_code == 200
    # no Retry-After header -- must fall back to the module's own backoff
    # schedule (starts at 2.0s, doubles), not a fixed/zero delay
    assert mock_sleep.call_args_list[0].args[0] == 2.0
    assert mock_sleep.call_args_list[1].args[0] == 4.0


def test_post_with_retry_raises_after_exhausting_max_retries():
    client = MagicMock()
    # 429 every single time -- should give up after max_retries, not loop forever
    client.post.side_effect = [_response(429) for _ in range(10)]

    with patch("codecheck.reviewers.openai_protocol.time.sleep"):
        with pytest.raises(httpx.HTTPStatusError):
            post_with_retry(client, "https://example.test", {}, max_retries=3)

    assert client.post.call_count == 4  # 1 initial attempt + 3 retries


def test_post_with_retry_does_not_retry_non_429_errors():
    # regression: retrying a 400/404/etc. would just get the same result --
    # only 429 (rate limit) is worth waiting out.
    client = MagicMock()
    client.post.return_value = _response(400)

    with patch("codecheck.reviewers.openai_protocol.time.sleep") as mock_sleep:
        with pytest.raises(httpx.HTTPStatusError):
            post_with_retry(client, "https://example.test", {})

    assert client.post.call_count == 1
    mock_sleep.assert_not_called()


def test_post_with_retry_ignores_unparseable_retry_after_and_uses_backoff():
    client = MagicMock()
    # an HTTP-date form (or garbage) Retry-After -- not the numeric-seconds
    # form this parses; must fall back to backoff instead of crashing.
    client.post.side_effect = [_response(429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}), _response(200)]

    with patch("codecheck.reviewers.openai_protocol.time.sleep") as mock_sleep:
        post_with_retry(client, "https://example.test", {})

    mock_sleep.assert_called_once_with(2.0)
