# Known v1 limitations (summary)

- `--config` is not auto-discovered; you must pass it explicitly or you silently
  get all-defaults.
- `openrouter`'s free-tier models vary in whether they actually honor forced
  tool-calling — if a specific free model on OpenRouter doesn't support
  function-calling, the review for that file will skip with "no
  report_findings tool call in response" rather than silently returning nothing
  useful.
- Ollama's OpenAI-compat layer doesn't reliably populate `tool_calls` even with
  `tool_choice: "required"` — confirmed against a real instance running
  `qwen2.5-coder:7b`, where identical requests sometimes landed the tool call
  properly and sometimes serialized it as text in `content` instead (recovered
  by the strict-JSON `content` fallback — see Tier 2 in [Architecture](Architecture.md)). A genuinely weak
  model (`llama3.2:1b`) sometimes produced malformed JSON there too, which is
  not recoverable without loosening the "never regex-scrape" rule, so it's left
  as a per-file skip rather than guessed at.
- `--pr`/`--repo-url` try the repo's existing git credential setup first (SSH
  key, `gh auth login`, `.netrc`, a credential helper) — `codecheck` doesn't
  manage GitHub auth itself. If that fails, it prompts for a username/token at
  an interactive terminal (3 attempts, never persisted — see "Private repos"
  in [CLI Reference](CLI-Reference.md)), but in a non-interactive context (CI,
  cron) it still just fails with a clear error rather than prompting. Base-branch
  auto-resolution additionally depends on the `gh` CLI being installed and
  authenticated; without it, pass `--base-ref` explicitly or it defaults to `main`.
- GitLab isn't supported for `--pr` — only GitHub's PR URL shape and
  `refs/pull/<n>/head` fetch convention are recognized. `--repo-url` alone
  (no `--pr`) works against any git host, GitLab included.
- No support for posting results back to GitHub (a PR comment, a check run) —
  this is one-directional: read from GitHub, review locally, report locally.
- Cloud tier makes one API call per target file, no batching; large runs mean
  many calls (mitigated by the `cloud.audit_file_cap` safety rail, which applies
  to both `audit --cloud` and `diff --cloud`).
- Cloud tier has no chunking for files over `max_file_lines` — they're skipped
  entirely, not partially reviewed.
- Aggregator dedupe is title-similarity only and misses same-issue findings with
  dissimilar wording (see the ruff/house-rule example in [Architecture](Architecture.md)).
- eslint and semgrep sub-runners are covered by tests with mocked subprocess
  output only — neither binary was available in this dev environment to verify
  against a real install.
- No `--post-comment` (GitHub PR comment posting) — not built.
- The local LLM tier has no context-building (symbol index, embeddings) — like
  the cloud tier, it just sends full file content + diff per file.
- LLM output doesn't reliably match a JSON schema's exact casing/types, even
  with forced tool-calling — confirmed against a real LM Studio server
  (`google/gemma-4-12b` returned `"High"`/`"Medium"` instead of the schema's
  lowercase enum, and could plausibly return non-integer line numbers). Handled
  via `Severity.parse()` (case-insensitive, falls back to `MEDIUM` for anything
  unrecognized) and `safe_int()` (falls back rather than crashing) in
  `reviewers/openai_protocol.py` — a malformed finding from any provider now
  degrades gracefully instead of crashing the whole tier.
- `tool_choice` is sent as the string `"required"`, not the object form pinning
  a specific function name — LM Studio's OpenAI-compat server only accepts
  `none`/`auto`/`required` and 400s on the object form. Since exactly one tool
  is ever registered, `"required"` has the same practical effect and is safe
  for the hosted providers too.
- LM Link (Preview) itself was observed to be flaky in testing — one request
  came back `{"error": "terminated"}`, and a follow-up failed to load the model
  at all with the remote entry briefly vanishing from `lms ps`. Neither case
  was a silent local fallback (`codecheck`'s device-resolution check correctly
  stayed accurate throughout), but a flaky link will surface as ordinary
  per-file skips in the report, not a guaranteed successful review. This is an
  LM Studio/LM Link reliability characteristic, not something `codecheck` can
  fix — retrying the run is the practical workaround for now.
- The local-execution confirmation gate (`lm_link.py`) only fires when
  `cfg.local.model` is set; it depends on the `lms` CLI being installed and on
  PATH. Without `lms`, every local-tier run is treated as "undetermined" and
  gated the same as a confirmed-local run (refuse non-interactively, prompt
  interactively) — this is deliberately the conservative default, not a bug,
  but it means `--force-local` becomes mandatory for non-interactive use on any
  machine without the `lms` CLI, even if a plain local-only setup (no LM Link
  involved at all) would otherwise be perfectly fine.
- `audit` mode has no file-count/language filtering beyond what each sub-runner
  already applies by extension (e.g. `.py` for ruff/house rules) — a very large
  monorepo will just take longer, there's no `--max-files` for the local-only tiers.
- vLLM's presence in the config docs (`provider: vllm`) is a documented,
  code-verified pattern — not a live-tested one. No real vLLM deployment was
  reached (needs an NVIDIA GPU not available in this dev environment).

---

[← Documentation index](Home.md)
