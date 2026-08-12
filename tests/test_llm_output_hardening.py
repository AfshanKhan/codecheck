"""Regression tests for real bugs found running against a live LM Studio server:
smaller/local models don't reliably match the JSON schema's exact casing or
types, and that must never crash the whole review.
"""

import httpx

from codecheck.models import Severity
from codecheck.reviewers.openai_protocol import format_http_error, safe_int


def test_severity_parse_normalizes_case():
    assert Severity.parse("High") == Severity.HIGH
    assert Severity.parse("MEDIUM") == Severity.MEDIUM
    assert Severity.parse(" low ") == Severity.LOW


def test_severity_parse_falls_back_to_medium_for_unknown_or_missing():
    assert Severity.parse("urgent") == Severity.MEDIUM
    assert Severity.parse(None) == Severity.MEDIUM
    assert Severity.parse("") == Severity.MEDIUM


def test_safe_int_coerces_numeric_strings():
    assert safe_int("5", default=1) == 5
    assert safe_int(5, default=1) == 5


def test_safe_int_falls_back_for_unparseable_or_missing():
    assert safe_int("line five", default=1) == 1
    assert safe_int(None, default=1) == 1
    assert safe_int(None, default=None) is None


def test_format_http_error_includes_response_body():
    # regression: str(e) alone on an HTTPStatusError omits the response body,
    # which is where the actual API-provided reason lives (rate limit
    # details, validation errors, etc.) -- confirmed against a real 400 from
    # Groq where the bare exception message gave no indication of the cause.
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    response = httpx.Response(400, request=request, text='{"error": "invalid request: bad tool_choice"}')
    error = httpx.HTTPStatusError("400 Bad Request", request=request, response=response)

    formatted = format_http_error(error)
    assert "invalid request: bad tool_choice" in formatted


def test_format_http_error_falls_back_to_str_for_non_status_errors():
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    error = httpx.ConnectError("Connection refused", request=request)

    formatted = format_http_error(error)
    assert "Connection refused" in formatted
