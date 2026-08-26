# Config file — full schema

```yaml
rules:
  enabled: true      # master switch for the whole rules tier
  ruff: true
  eslint: true
  semgrep: true
  house_rules: true
  test_coverage: true   # RULE-017: diff changes app code but touches no test file (diff mode only)
  disabled_checks: []   # e.g. ["RULE-009", "RUFF-F401"] -- drop specific check IDs from the report
                         # regardless of which sub-runner produced them, without disabling that
                         # whole sub-runner
  extra_checks: []      # e.g. ["your_package.checks:YourCheck"] -- dotted "module:ClassName"
                         # paths to project-specific HouseCheck subclasses, run alongside the
                         # built-ins -- see "Adding project-specific checks" below

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
  fail_on_severity: "high"   # one of: info, low, medium, high, critical -- or use --gate on the CLI
                              # for a named profile (strict/standard/relaxed) instead of a raw value

suggestions:                 # only used when --suggest-fixes is passed (see CLI Reference)
  max_per_run: 5              # cap on how many findings get a fix suggestion per run, highest severity first
  exclude_checks: []          # e.g. ["RULE-009"] -- check IDs never sent to the LLM for a suggestion
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

### Adding project-specific checks (`rules.extra_checks`)

The 18 built-in house rules ([Architecture](Architecture.md#house-rules-checks))
cover general Python/JS/Frappe patterns, but a specific project or org often
has its own conventions worth enforcing automatically — an internal API
that's easy to misuse, a naming convention, a deprecated helper nobody should
call anymore. `rules.extra_checks` lets you register your own `HouseCheck`
subclasses without forking `codecheck` to add them to the built-in list:

```yaml
rules:
  extra_checks:
    - "your_package.checks.security:NoRawShellCommand"
    - "your_package.checks.style:RequireDocTypeNamingConvention"
```

Each entry is a dotted `"module.path:ClassName"` string — importable from
wherever `codecheck` runs (installed alongside it, or just on `PYTHONPATH`),
implementing the same `HouseCheck` interface as every built-in check (see
"Adding a new house rule" in [Architecture](Architecture.md#house-rules-checks)):
a `check_id`/`title`/`severity`, and `check_file(file_path, content,
changed_lines) -> list[Finding]`. Loaded checks run in the same per-file loop
as the built-ins — `codecheck` doesn't distinguish between them once loaded.

A path that fails to import, isn't a `HouseCheck` subclass, or can't be
instantiated with no arguments is reported the same way a missing linter
binary is — a line under "Skipped" in the report, not a crash that takes the
built-in checks down with it.

---

[← Documentation index](Home.md)
