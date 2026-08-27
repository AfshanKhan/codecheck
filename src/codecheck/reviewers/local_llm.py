"""Tier 2: local LLM reviewer, against any OpenAI-compatible local server (LM
Studio, Ollama, etc.). With the default provider presets: no API key, no
network call beyond localhost -- a custom `local.base_url`/`api_key_env`
can point this at a remote endpoint instead. Built on the same
`OpenAIProtocolReviewer` base as the cloud tier's OpenAI-compatible
backend; only endpoint resolution differs."""

from __future__ import annotations

import os
from pathlib import Path

from codecheck.reviewers.openai_protocol import OpenAIProtocolReviewer

_LOCAL_PROVIDER_BASE_URLS = {
    "lm_studio": "http://localhost:1234/v1/chat/completions",
    "ollama": "http://localhost:11434/v1/chat/completions",
}


class LocalLLMReviewer(OpenAIProtocolReviewer):
    tier = "local_llm"
    name = "local_llm"
    check_id_prefix = "LOCAL"
    _disabled_message = "local LLM tier disabled"

    def _resolved_base_url(self) -> str | None:
        if self.config.base_url:
            return self.config.base_url
        return _LOCAL_PROVIDER_BASE_URLS.get(self.config.provider)

    def _resolved_api_key_env(self) -> str | None:
        return self.config.api_key_env or None

    def is_available(self, repo_path: Path) -> tuple[bool, str | None]:
        if not self.config.enabled:
            return False, self._disabled_message
        if not self._resolved_base_url():
            return False, (
                f"no base_url configured for provider {self.config.provider!r} "
                f"(set local.base_url, or use a known provider: {', '.join(_LOCAL_PROVIDER_BASE_URLS)})"
            )
        if not self.config.model:
            return False, "local.model not set — specify the model currently loaded in your local server"
        key_env = self._resolved_api_key_env()
        if key_env and not os.environ.get(key_env):
            return False, f"{key_env} env var not set"
        return True, None
