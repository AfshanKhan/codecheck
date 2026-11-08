"""Config schema and loading: config.yaml -> Config, with CLI flags overriding config values."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator

from codecheck.models import Severity


class RulesConfig(BaseModel):
    enabled: bool = True
    ruff: bool = True
    eslint: bool = True
    semgrep: bool = True
    # Local path to a semgrep rules directory (e.g. a clone of
    # frappe/semgrep-rules), passed as `semgrep --config <path>`. Falls back
    # to semgrep's own `--config=auto` (its hosted registry) when unset.
    semgrep_config: str | None = None
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
    # anthropic | groq | mistral | cerebras | openrouter | google | openai_compatible (custom base_url)
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    # Defaults per-provider if left unset (e.g. GROQ_API_KEY for provider=groq). Never the key itself.
    api_key_env: str | None = None
    # Required for provider="openai_compatible"; optional override for the named free providers.
    base_url: str | None = None
    # Reasoning effort sent to the provider (low | medium | high); None = the provider's own default.
    effort: Literal["low", "medium", "high"] | None = None
    max_file_lines: int = 800
    audit_file_cap: int = 50  # max files the cloud tier will review in one `audit` run without --force-cloud
    # Leave out tests, bundled/generated files, empty files and boilerplate (see codecheck.noise).
    skip_noise: bool = False
    # Only files with these extensions go to the cloud tier (None = every file). Rules still see all files.
    extensions: list[str] | None = None
    # Stop starting new files after this many seconds; the rest are listed as skipped and the report is still written.
    time_budget_seconds: float | None = None
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
    skip_noise: bool = False  # same meaning as cloud.skip_noise
    extensions: list[str] | None = None  # same meaning as cloud.extensions
    # Higher than the cloud default -- CPU inference can take minutes.
    request_timeout_seconds: float = 300.0


class ThresholdsConfig(BaseModel):
    fail_on_severity: str = "high"

    @field_validator("fail_on_severity")
    @classmethod
    def _valid_severity(cls, value: str) -> str:
        # Fail at load time, not after a full scan when Severity(...) finally runs.
        normalized = value.strip().lower()
        if normalized not in {s.value for s in Severity}:
            allowed = ", ".join(s.value for s in Severity)
            raise ValueError(f"must be one of {allowed}, got {value!r}")
        return normalized


class Config(BaseModel):
    rules: RulesConfig = RulesConfig()
    cloud: CloudConfig = CloudConfig()
    local: LocalConfig = LocalConfig()
    thresholds: ThresholdsConfig = ThresholdsConfig()
    suggestions: SuggestionsConfig = SuggestionsConfig()


def load_config(config_path: Path | None) -> Config:
    if config_path is None:
        return Config()
    if not config_path.is_file():
        # An explicit --config that doesn't exist is a typo, not "use the defaults".
        raise FileNotFoundError(f"config file not found: {config_path}")
    with config_path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Config.model_validate(raw)
