"""Shared OpenAI-compatible chat-completions protocol: request shape,
forced-tool-call JSON schema, and per-file skip/loop logic used by every
cloud/local reviewer that speaks this protocol."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import httpx

from codecheck.diff import read_file_content
from codecheck.models import Finding, ReviewTarget, Severity
from codecheck.reviewers.base import Reviewer

DEFAULT_MAX_RETRIES = 5
_INITIAL_RETRY_DELAY_SECONDS = 2.0
_MAX_RETRY_DELAY_SECONDS = 60.0


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parses a plain-integer-seconds Retry-After header. The HTTP-date form
    isn't handled; falls back to backoff for that."""
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def post_with_retry(
    client: httpx.Client, url: str, json_payload: dict, max_retries: int = DEFAULT_MAX_RETRIES
) -> httpx.Response:
    """POST with automatic retry-with-backoff on HTTP 429. Honors the
    server's Retry-After header, capped at _MAX_RETRY_DELAY_SECONDS. Any
    non-429 error is raised immediately."""
    delay = _INITIAL_RETRY_DELAY_SECONDS
    attempt = 0
    while True:
        response = client.post(url, json=json_payload)
        if response.status_code != 429 or attempt >= max_retries:
            response.raise_for_status()
            return response
        wait = min(_parse_retry_after(response) or delay, _MAX_RETRY_DELAY_SECONDS)
        time.sleep(wait)
        delay = min(delay * 2, _MAX_RETRY_DELAY_SECONDS)
        attempt += 1

SYSTEM_PROMPT = """You are a senior code reviewer. You will be given a file's full \
content, and possibly a unified diff of a recent change to it. Report findings \
focused on:

- Logic bugs and correctness issues
- Edge cases that aren't handled (empty input, None, concurrency, off-by-one)
- Security issues (injection, unsafe deserialization, secrets, auth/authz gaps)
- Frappe/ERPNext-specific anti-patterns where applicable (unsafe frappe.db.sql usage, \
missing permission checks, direct DB writes bypassing the ORM's validation hooks)

If a diff is included, only report findings about lines that were actually changed \
in it. If no diff is included, this is a full-file audit — review the entire file. \
Do not comment on style or formatting (a separate linter handles that). If there is \
nothing worth flagging, report zero findings. Call the report_findings tool with \
your findings; do not respond with prose."""

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["info", "low", "medium", "high", "critical"],
                    },
                    "title": {"type": "string"},
                    "explanation": {"type": "string"},
                    "line_start": {"type": "integer"},
                    "line_end": {"type": "integer"},
                    "suggestion": {"type": "string"},
                },
                "required": ["severity", "title", "explanation", "line_start"],
            },
        }
    },
    "required": ["findings"],
}

OPENAI_FINDINGS_FUNCTION = {
    "name": "report_findings",
    "description": "Report code review findings for this file.",
    "parameters": FINDINGS_SCHEMA,
}


def safe_int(value, default: int | None) -> int | None:
    """Coerces a loosely-typed LLM value (e.g. a line number as a string) or
    falls back rather than crashing."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_user_message(target: ReviewTarget, content: str) -> str:
    message = f"File: {target.path}\nStatus: {target.status}\n\nFull file content:\n```\n{content}\n```"
    if target.diff_text:
        message += f"\n\nUnified diff of the change:\n```diff\n{target.diff_text}\n```"
    else:
        message += (
            "\n\nNo diff included — this is a full-file audit, not a diff review. "
            "Review the entire file."
        )
    return message


def format_http_error(e: httpx.HTTPError) -> str:
    """Includes the response body -- httpx.HTTPError's str() alone omits it."""
    if isinstance(e, httpx.HTTPStatusError):
        body = e.response.text.strip()
        if body:
            return f"{e} — {body[:500]}"
    return str(e)


_DIFF_LINE_TOLERANCE = 2


def within_diff_scope(target: ReviewTarget, finding: Finding) -> bool:
    """In diff mode, only findings on (or near) an actually-changed line are
    kept -- enforced programmatically, not just via the prompt."""
    if target.changed_lines is None:
        return True  # audit mode: every line is in scope
    end = finding.line_end or finding.line_start
    return any(
        line in target.changed_lines
        for line in range(finding.line_start - _DIFF_LINE_TOLERANCE, end + _DIFF_LINE_TOLERANCE + 1)
    )


def _extract_findings_from_content(content: str | None) -> list[dict] | None:
    """Strict json.loads fallback for a tool call serialized into `content`.
    Returns None if it doesn't parse to exactly the expected shape."""
    if not content:
        return None
    try:
        parsed = json.loads(content.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    if "findings" in parsed and isinstance(parsed["findings"], list):
        findings = parsed["findings"]
        return findings if all(isinstance(f, dict) for f in findings) else None

    if parsed.get("name") == "report_findings":
        arguments = parsed.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return None
        if isinstance(arguments, dict) and isinstance(arguments.get("findings"), list):
            findings = arguments["findings"]
            return findings if all(isinstance(f, dict) for f in findings) else None

    return None


class OpenAIProtocolReviewer(Reviewer):
    check_id_prefix: ClassVar[str] = "LLM"
    _disabled_message: ClassVar[str] = "tier disabled"

    def __init__(self, config, client: httpx.Client | None = None):
        self.config = config
        self._client = client
        self.skipped_files: list[tuple[str, str]] = []

    def _resolved_base_url(self) -> str | None:
        raise NotImplementedError

    def _resolved_api_key_env(self) -> str | None:
        raise NotImplementedError

    def is_available(self, repo_path: Path) -> tuple[bool, str | None]:
        if not self.config.enabled:
            return False, self._disabled_message
        base_url = self._resolved_base_url()
        if not base_url:
            return False, "no base_url configured"
        key_env = self._resolved_api_key_env()
        if key_env and not os.environ.get(key_env):
            return False, f"{key_env} env var not set"
        return True, None

    def _get_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        headers = {"content-type": "application/json"}
        key_env = self._resolved_api_key_env()
        if key_env:
            api_key = os.environ.get(key_env)
            if api_key:
                headers["authorization"] = f"Bearer {api_key}"
        timeout = getattr(self.config, "request_timeout_seconds", 120.0)
        return httpx.Client(headers=headers, timeout=timeout)

    def _review_file(
        self, client: httpx.Client, target: ReviewTarget, content: str
    ) -> tuple[list[dict], str | None]:
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_message(target, content)},
            ],
            "tools": [{"type": "function", "function": OPENAI_FINDINGS_FUNCTION}],
            "tool_choice": "required",
            "max_tokens": 4096,
        }

        try:
            response = post_with_retry(client, self._resolved_base_url(), payload)
        except httpx.HTTPError as e:
            return [], f"API request failed: {format_http_error(e)}"

        try:
            data = response.json()
        except json.JSONDecodeError:
            return [], "response was not valid JSON"
        if not isinstance(data, dict):
            return [], "response JSON was not an object"

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            return [], "no choices in response"
        if not isinstance(message, dict):
            return [], "message in response was not an object"

        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            tool_calls = []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict) or function.get("name") != "report_findings":
                continue
            try:
                args = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                return [], "could not parse tool call arguments as JSON"
            if not isinstance(args, dict):
                return [], "tool call arguments were not a JSON object"
            findings = args.get("findings", [])
            if not isinstance(findings, list):
                return [], "tool call 'findings' was not a JSON array"
            if not all(isinstance(finding, dict) for finding in findings):
                return [], "tool call 'findings' contained a non-object element"
            return findings, None

        # Some servers serialize the tool call into `content` instead of
        # tool_calls -- strict json.loads, no prose-scraping.
        findings = _extract_findings_from_content(message.get("content"))
        if findings is not None:
            return findings, None

        return [], "no report_findings tool call in response"

    def _finding_from_raw(self, raw: dict, file_path: str, index: int) -> Finding:
        severity = Severity.parse(raw.get("severity"))
        return Finding(
            check_id=f"{self.check_id_prefix}-{index:03d}",
            tier=self.tier,
            source=self.name,
            severity=severity,
            title=raw.get("title", f"{self.name} finding"),
            explanation=raw.get("explanation", ""),
            file=file_path,
            line_start=safe_int(raw.get("line_start"), default=1),
            line_end=safe_int(raw.get("line_end"), default=None),
            suggestion=raw.get("suggestion"),
            raw=raw,
        )

    def review(
        self,
        targets: list[ReviewTarget],
        repo_path: Path,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> list[Finding]:
        """on_progress, if given, is called as on_progress(file_path, outcome)
        after each file is processed."""
        self.skipped_files = []
        client = self._get_client()
        findings: list[Finding] = []
        finding_counter = 0

        for target in targets:
            if target.status == "deleted":
                continue
            content = read_file_content(repo_path, target)
            if content is None:
                self.skipped_files.append((target.path, "could not read file content"))
                if on_progress:
                    on_progress(target.path, "skipped: could not read file content")
                continue
            line_count = content.count("\n") + 1
            if line_count > self.config.max_file_lines:
                reason = f"file too large ({line_count} lines > {self.config.max_file_lines})"
                self.skipped_files.append((target.path, reason))
                if on_progress:
                    on_progress(target.path, f"skipped: {reason}")
                continue

            raw_findings, error = self._review_file(client, target, content)
            if error:
                self.skipped_files.append((target.path, error))
                if on_progress:
                    on_progress(target.path, f"skipped: {error}")
                continue

            file_finding_count = 0
            for raw in raw_findings:
                finding_counter += 1
                finding = self._finding_from_raw(raw, target.path, finding_counter)
                if within_diff_scope(target, finding):
                    findings.append(finding)
                    file_finding_count += 1
            if on_progress:
                noun = "finding" if file_finding_count == 1 else "findings"
                on_progress(target.path, f"{file_finding_count} {noun}")

        return findings
