# Config file — full schema

```yaml
rules:
  enabled: true      # master switch for the whole rules tier
  ruff: true
  eslint: true
  semgrep: true
  house_rules: true
  test_coverage: true   # RULE-017: diff changes app code but touches no test file (diff mode only)

cloud:
  enabled: false          # can also be turned on with --cloud
  provider: anthropic     # anthropic | groq | mistral | cerebras | openrouter | openai_compatible
  model: "claude-sonnet-4-6"
  api_key_env: "ANTHROPIC_API_KEY"   # name of the env var holding the key; omit to use the provider's default
  base_url: null           # only used by provider: openai_compatible (custom endpoint, e.g. LM Studio)
  max_file_lines: 800     # files larger than this are skipped by the cloud tier
  audit_file_cap: 50      # cloud tier (audit OR diff) refuses to run over this many files without --force-cloud
  request_timeout_seconds: 120   # per-request HTTP timeout (seconds); hosted APIs are fast

local:
  enabled: false          # can also be turned on with --local
  provider: lm_studio     # lm_studio | ollama | openai_compatible (custom base_url)
  model: ""               # required — must match the model currently loaded in your local server
  base_url: null          # only used by provider: openai_compatible, or to override a preset
  api_key_env: null       # set only if your local server requires auth (most don't)
  max_file_lines: 2000    # higher than the cloud default — local inference is free
  request_timeout_seconds: 300   # CPU inference can take minutes per file; raise for slower hardware

thresholds:
  fail_on_severity: "high"   # one of: info, low, medium, high, critical
```

**Important gap to know about:** `load_config()` ([config.py](../src/codecheck/config.py))
only reads a config file if you pass `--config` explicitly. If you omit the flag,
it silently falls back to all-defaults — it does **not** look for `./config.yaml`
in the repo or cwd automatically. If you have a `config.yaml` sitting next to
where you run the command, you still need `--config config.yaml` or it's ignored.

### Setting up cloud AI review (free providers)

`cloud.provider` isn't Anthropic-only. Groq, Mistral, Cerebras, and OpenRouter all
issue genuinely free API keys — no credit card, no prepaid balance — on the same
OpenAI-compatible chat-completions protocol (function-calling included), so
`codecheck` talks to all of them through one implementation,
`OpenAICompatibleCloudReviewer` in
[`reviewers/cloud_llm.py`](../src/codecheck/reviewers/cloud_llm.py):

```yaml
cloud:
  enabled: true
  provider: groq
  model: "llama-3.3-70b-versatile"
  # api_key_env defaults to GROQ_API_KEY for this provider — no need to set it
```

| `provider` | Default `base_url` | Default `api_key_env` |
|---|---|---|
| `anthropic` | (Anthropic Messages API) | `ANTHROPIC_API_KEY` |
| `groq` | `api.groq.com/openai/v1/chat/completions` | `GROQ_API_KEY` |
| `mistral` | `api.mistral.ai/v1/chat/completions` | `MISTRAL_API_KEY` |
| `cerebras` | `api.cerebras.ai/v1/chat/completions` | `CEREBRAS_API_KEY` |
| `openrouter` | `openrouter.ai/api/v1/chat/completions` | `OPENROUTER_API_KEY` |
| `openai_compatible` | **required**, set `cloud.base_url` yourself | none by default — set `cloud.api_key_env` if the endpoint needs auth |

`provider: openai_compatible` + `cloud.base_url` also covers a local LM Studio
server (or anything else speaking the same protocol) — set `api_key_env: ""` if
the endpoint doesn't require auth at all. All the usual safety behavior
(`max_file_lines`, `audit_file_cap`, per-file skip-not-crash on errors) applies
identically regardless of provider — `_BaseCloudReviewer` in `cloud_llm.py` owns
that loop, and both backends plug into it.

`build_cloud_reviewer(config)` is the factory that picks `AnthropicCloudReviewer`
vs. `OpenAICompatibleCloudReviewer` based on `cloud.provider` — this is what
`cli.py` calls, so switching providers is a config change only, never a code change.

**Self-hosted vLLM** (the common choice for hosting your own models on cloud
GPU infrastructure) works the same way, and needs no dedicated preset — verified
directly: `provider` is just a free-form label for presets/error messages, and
an explicit `cloud.base_url` always wins over any preset lookup regardless of
what the provider string is set to. Unlike Groq/Mistral/Cerebras/OpenRouter,
which each have exactly one fixed public API URL, a self-hosted vLLM deployment
has no canonical address — it's wherever you deployed it — so there's
deliberately no preset with a fabricated default `base_url` for it:

```yaml
cloud:
  provider: vllm
  base_url: "https://your-vllm-deployment.example.com/v1/chat/completions"
  model: "your-model-name"
  api_key_env: "VLLM_API_KEY"   # only if your deployment requires auth
```

---

[← Documentation index](Home.md)
