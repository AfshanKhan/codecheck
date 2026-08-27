"""Config schema and loading: config.yaml -> Config, with CLI flags overriding config values."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel


class RulesConfig(BaseModel):
    enabled: bool = True
    ruff: bool = True
    eslint: bool = True
    semgrep: bool = True
    house_rules: bool = True
    test_coverage: bool = True
    secrets_scan: bool = True
    # Check IDs to drop from the final report regardless of source.
    disabled_checks: list[str] = []
    # Dotted import paths ("your_package.module:YourCheckClass") to extra
    # HouseCheck subclasses to run alongside the built-in ones.
    extra_checks: list[str] = []


class SuggestionsConfig(BaseModel):
    # Caps the fix-suggestion pass to at most this many findings per run.
    max_per_run: int = 5
    # Check IDs to never send to the LLM for a suggestion.
    exclude_checks: list[str] = []


class CloudConfig(BaseModel):
    enabled: bool = False
    # anthropic | groq | mistral | cerebras | openrouter | openai_compatible (custom base_url)
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    # Defaults per-provider if left unset (e.g. GROQ_API_KEY for provider=groq). Never the key itself.
    api_key_env: str | None = None
    # Required for provider="openai_compatible"; optional override for the named free providers.
    base_url: str | None = None
    max_file_lines: int = 800
    audit_file_cap: int = 50  # max files the cloud tier will review in one `audit` run without --force-cloud
    request_timeout_seconds: float = 120.0  # hosted APIs are fast; raise this if a provider is slow


class LocalConfig(BaseModel):
    enabled: bool = False
    # lm_studio | ollama | openai_compatible (custom base_url)
    provider: str = "lm_studio"
    # Defaults per-provider if left unset. Only required for provider="openai_compatible".
    base_url: str | None = None
    model: str = ""  # required — whatever model is currently loaded in the local server
    # Local servers usually need no auth; set this only if yours does.
    api_key_env: str | None = None
    max_file_lines: int = 2000
    # Higher than the cloud default -- CPU inference can take minutes.
    request_timeout_seconds: float = 300.0


class ThresholdsConfig(BaseModel):
    fail_on_severity: str = "high"


class Config(BaseModel):
    rules: RulesConfig = RulesConfig()
    cloud: CloudConfig = CloudConfig()
    local: LocalConfig = LocalConfig()
    thresholds: ThresholdsConfig = ThresholdsConfig()
    suggestions: SuggestionsConfig = SuggestionsConfig()


def load_config(config_path: Path | None) -> Config:
    if config_path is None or not config_path.is_file():
        return Config()
    with config_path.open() as f:
        raw = yaml.safe_load(f) or {}
    return Config.model_validate(raw)
