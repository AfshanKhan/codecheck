# codecheck — documentation

Reference and deep-dive docs for `codecheck`. New here? Start with the
[README](../README.md) for install + first run, then come back for the details.

## Contents

- **[Installation](Installation.md)** — every install option (dev, standalone,
  wheel/pip, published release) and how to publish a release.
- **[CLI reference](CLI-Reference.md)** — full `diff` / `audit` flag tables, PR
  reviewing, private-repo credential handling, exit codes, `--help` / `--version`.
- **[Configuration](Configuration.md)** — the full `config.yaml` schema and
  cloud/local provider setup (Anthropic, Groq, Mistral, Cerebras, OpenRouter,
  vLLM, LM Studio, Ollama).
- **[Architecture & internals](Architecture.md)** — how the tiers, rules engine,
  house rules, LLM reviewers, aggregator, and reporters actually work; project
  layout; running tests.
- **[Local LLM model guide](Architecture.md#which-model-to-run-locally-as-of-august-2026)** —
  hardware-tiered model suggestions for the local tier (inside Architecture).
- **[Security](Security.md)** — what running `codecheck` against untrusted code
  does, what's hardened, and how private-repo credential prompting works. See
  also the top-level [SECURITY.md](../SECURITY.md).
- **[Verification status](Verification-Status.md)** — what's been tested against
  real, live instances vs. what's only expected to work.
- **[Known limitations](Known-Limitations.md)** — the honest v1 caveats.
