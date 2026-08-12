"""Tier 3: cloud LLM reviewer. Calls a hosted LLM API with the diff plus full file
content per changed file, forcing structured JSON output via tool-calling so we
never have to regex-scrape free text.

Two backends:

- `AnthropicCloudReviewer` — the Anthropic Messages API (tool-use), its own
  small per-file loop since the request/response shape doesn't match OpenAI's.
- `OpenAICompatibleCloudReviewer` — built on the shared
  `reviewers.openai_protocol.OpenAIProtocolReviewer` (also used by the local LLM
  tier): any OpenAI-compatible chat-completions endpoint (function-calling).
  Groq, Mistral, Cerebras, and OpenRouter all offer genuinely free API keys (no
  prepaid balance) on this protocol, plus any custom endpoint via
  provider="openai_compatible" + base_url.

`build_cloud_reviewer()` picks the right one from config.cloud.provider.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from codecheck.config import CloudConfig
from codecheck.diff import read_file_content
from codecheck.models import Finding, ReviewTarget, Severity
from codecheck.reviewers.base import Reviewer
from codecheck.reviewers.openai_protocol import (
    SYSTEM_PROMPT,
    OpenAIProtocolReviewer,
    build_user_message,
    format_http_error,
    safe_int,
    within_diff_scope,
)

_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_DEFAULT_API_KEY_ENV = "ANTHROPIC_API_KEY"

_ANTHROPIC_FINDINGS_TOOL = {
    "name": "report_findings",
    "description": "Report code review findings for this file.",
    "input_schema": {
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
    },
}

# Free (no prepaid balance required) providers speaking the OpenAI chat-completions
# protocol. provider="openai_compatible" + cloud.base_url covers anything else.
_OPENAI_COMPATIBLE_PRESETS: dict[str, dict[str, str]] = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "api_key_env": "GROQ_API_KEY",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1/chat/completions",
        "api_key_env": "MISTRAL_API_KEY",
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1/chat/completions",
        "api_key_env": "CEREBRAS_API_KEY",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "api_key_env": "OPENROUTER_API_KEY",
    },
}


def exceeds_audit_cap(targets: list[ReviewTarget], config: CloudConfig, force: bool) -> int | None:
    """Whole-repo audits can mean one cloud API call per file in the repo, so this
    is a safety rail against an accidental huge bill. Returns the eligible file
    count if it exceeds config.audit_file_cap and force wasn't passed (the caller
    should refuse to run cloud review), or None if it's fine to proceed.
    """
    eligible = [t for t in targets if t.status != "deleted"]
    if not force and len(eligible) > config.audit_file_cap:
        return len(eligible)
    return None


class AnthropicCloudReviewer(Reviewer):
    tier = "cloud_llm"
    name = "cloud_llm"

    def __init__(self, config: CloudConfig, client: httpx.Client | None = None):
        self.config = config
        self._client = client
        self.skipped_files: list[tuple[str, str]] = []

    def is_available(self, repo_path: Path) -> tuple[bool, str | None]:
        if not self.config.enabled:
            return False, "cloud tier disabled"
        key_env = self.config.api_key_env or _ANTHROPIC_DEFAULT_API_KEY_ENV
        if not os.environ.get(key_env):
            return False, f"{key_env} env var not set"
        return True, None

    def _get_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        key_env = self.config.api_key_env or _ANTHROPIC_DEFAULT_API_KEY_ENV
        api_key = os.environ[key_env]
        return httpx.Client(
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            # was hard-coded to 60.0 -- silently ignored cloud.request_timeout_seconds,
            # so raising the config value past 60s had no effect for this provider.
            timeout=getattr(self.config, "request_timeout_seconds", 120.0),
        )

    def review(self, targets: list[ReviewTarget], repo_path: Path) -> list[Finding]:
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
                continue
            line_count = content.count("\n") + 1
            if line_count > self.config.max_file_lines:
                self.skipped_files.append(
                    (target.path, f"file too large ({line_count} lines > {self.config.max_file_lines})")
                )
                continue

            raw_findings, error = self._review_file(client, target, content)
            if error:
                self.skipped_files.append((target.path, error))
                continue

            for raw in raw_findings:
                finding_counter += 1
                finding = _anthropic_finding_from_raw(raw, target.path, finding_counter)
                if within_diff_scope(target, finding):
                    findings.append(finding)

        return findings

    def _review_file(
        self, client: httpx.Client, target: ReviewTarget, content: str
    ) -> tuple[list[dict], str | None]:
        payload = {
            "model": self.config.model,
            "max_tokens": 4096,
            "system": SYSTEM_PROMPT,
            "tools": [_ANTHROPIC_FINDINGS_TOOL],
            "tool_choice": {"type": "tool", "name": "report_findings"},
            "messages": [{"role": "user", "content": build_user_message(target, content)}],
        }

        try:
            response = client.post(_ANTHROPIC_API_URL, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as e:
            return [], f"API request failed: {format_http_error(e)}"

        try:
            data = response.json()
        except json.JSONDecodeError:
            return [], "response was not valid JSON"
        if not isinstance(data, dict):
            return [], "response JSON was not an object"

        content_blocks = data.get("content")
        if not isinstance(content_blocks, list):
            content_blocks = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "report_findings":
                tool_input = block.get("input")
                if not isinstance(tool_input, dict):
                    return [], "tool_use block 'input' was not a JSON object"
                findings = tool_input.get("findings", [])
                if not isinstance(findings, list):
                    return [], "tool_use block 'findings' was not a JSON array"
                return findings, None
        return [], "no tool_use block in response"


def _anthropic_finding_from_raw(raw: dict, file_path: str, index: int) -> Finding:
    severity = Severity.parse(raw.get("severity"))
    return Finding(
        check_id=f"CLOUD-{index:03d}",
        tier="cloud_llm",
        source="cloud_llm",
        severity=severity,
        title=raw.get("title", "Cloud LLM finding"),
        explanation=raw.get("explanation", ""),
        file=file_path,
        line_start=safe_int(raw.get("line_start"), default=1),
        line_end=safe_int(raw.get("line_end"), default=None),
        suggestion=raw.get("suggestion"),
        raw=raw,
    )


class OpenAICompatibleCloudReviewer(OpenAIProtocolReviewer):
    """Any OpenAI-compatible chat-completions endpoint: the free-tier hosted
    providers in _OPENAI_COMPATIBLE_PRESETS, or a fully custom endpoint via
    provider="openai_compatible" + cloud.base_url.
    """

    tier = "cloud_llm"
    name = "cloud_llm"
    check_id_prefix = "CLOUD"
    _disabled_message = "cloud tier disabled"

    def _preset(self) -> dict[str, str] | None:
        return _OPENAI_COMPATIBLE_PRESETS.get(self.config.provider)

    def _resolved_base_url(self) -> str | None:
        if self.config.base_url:
            return self.config.base_url
        preset = self._preset()
        return preset["base_url"] if preset else None

    def _resolved_api_key_env(self) -> str | None:
        if self.config.api_key_env:
            return self.config.api_key_env
        preset = self._preset()
        return preset["api_key_env"] if preset else None

    def is_available(self, repo_path: Path) -> tuple[bool, str | None]:
        if not self.config.enabled:
            return False, self._disabled_message
        if self._resolved_base_url() is None:
            return False, (
                f"no base_url configured for provider {self.config.provider!r} "
                f"(set cloud.base_url, or use a known provider: "
                f"{', '.join(_OPENAI_COMPATIBLE_PRESETS)})"
            )
        key_env = self._resolved_api_key_env()
        if key_env and not os.environ.get(key_env):
            return False, f"{key_env} env var not set"
        return True, None


def build_cloud_reviewer(config: CloudConfig, client: httpx.Client | None = None) -> Reviewer:
    if config.provider == "anthropic":
        return AnthropicCloudReviewer(config, client=client)
    return OpenAICompatibleCloudReviewer(config, client=client)
