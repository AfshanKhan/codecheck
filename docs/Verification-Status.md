# What's actually verified vs. what's expected to work

"Built on the same code path as X" is not the same as "tested." Two providers
already claiming OpenAI compatibility (LM Studio, Ollama) both turned out to
have real quirks once actually tested live (see Tiers 2 and 3 in [Architecture](Architecture.md)) — so
nothing in this table is marked ✅ unless it was run against a real, live
instance and produced a real result, not just passed a mocked unit test.

| Component | Status | Notes |
|---|---|---|
| Rules engine — ruff | ✅ Verified | Real installs, real findings, on this machine. |
| Rules engine — house rules (`RULE-001`, `RULE-002`) | ✅ Verified | Real AST checks, real files. |
| Rules engine — eslint | ⚠️ Untested | No JS/TS project + eslint install available in dev — covered by mocked-subprocess tests only. |
| Rules engine — semgrep | ⚠️ Untested | Same — mocked tests only, no real binary run. |
| Tier 2 — LM Studio (local) | ✅ Verified | Real running instance; found and fixed 2 real bugs (`tool_choice` format, severity casing). |
| Tier 2 — LM Studio + LM Link (remote device) | ✅ Verified | Real second machine over LM Link; found and fixed 1 real bug (device misdetection when a model is remote-only). |
| Tier 2 — Ollama | ✅ Verified | Real running instance; found and fixed 1 real bug (tool call landing in `content` instead of `tool_calls`). |
| Tier 2 — `openai_compatible` / llama.cpp (`llama-server`) | ⚠️ Tested, unreliable | Real `llama-server` + Qwen2.5-Coder-7B-Instruct-GGUF (`--jinja`): found and fixed 2 real bugs (missing `max_tokens` causing unbounded generation, and a 120s timeout too short for real generation speed here — both fixed and benefit every provider). But forced tool-calling itself never worked in 2/2 tries — the model reasoned out correct findings but emitted malformed JSON as prose (`<{{"name":...`) instead of using `tool_calls`, which our strict-parse fallback correctly declined to guess at. Note: this session initially claimed the run was "CPU only" — that was an unverified assumption, later contradicted by directly observed GPU activity; see Tier 2 in [Architecture](Architecture.md) for the correction. |
| Tier 3 — Groq | ✅ Verified | Real API key, real account; found and fixed 2 real bugs (diff-scope enforcement, missing error detail). |
| Tier 3 — Anthropic | ⚠️ Untested | Mocked-client tests only — never a real API call. |
| Tier 3 — Mistral | ⚠️ Untested | Never tried at all. |
| Tier 3 — Cerebras | ⚠️ Untested | Never tried at all. |
| Tier 3 — OpenRouter | ⚠️ Untested, extra caution | Never tried, and it proxies to many underlying models — some free ones are already known to not reliably support forced tool-calling (see Tier 3 in [Architecture](Architecture.md)). |
| Tier 3 — `openai_compatible` (custom cloud endpoint) | ⚠️ Untested | Same code path as Groq, but no other real endpoint was tried. |
| Tier 3 — self-hosted vLLM | ⚠️ Config verified, no real vLLM instance tested | Confirmed by direct code inspection (not a live vLLM server) that an arbitrary `provider` name + explicit `base_url`/`api_key_env` resolves and gates correctly with zero code changes — vLLM has no fixed public URL, so it was never going to get a dedicated preset the way Groq et al. did. No actual vLLM deployment (this needs an NVIDIA GPU, not available here) was reached. |
| `--pr` (GitHub PR fetch, both the full-URL and bare-number forms) | ✅ Verified | Real `codecheck diff --pr <full github.com PR URL>` against a real public repo: correctly parsed the repo + PR number out of the URL, cloned it, fetched `refs/pull/<n>/head`, resolved the merge-base, and reviewed a real 88-file diff end to end. Also confirmed the merge-base semantics are correct — pointing it at a PR already merged into the target branch correctly reported zero changes rather than a stale diff. |
| `--repo-url` (clone), `--branch` | ✅ Verified | Real clone of a public GitHub repo, both with and without `--branch` (confirmed `--single-branch` picks the exact branch requested, not the remote's default). |
| `uv tool install` / `uvx` (standalone install) | ✅ Verified | Installed for real, ran `codecheck audit` from outside the repo with no `uv run`. Confirmed `--editable` picks up source changes with zero reinstall (edited a string, reran, saw it immediately) — that's the recommended install mode, see [Installation](Installation.md). |
| Plain `pip install` (no `uv`) | ✅ Verified | Built a real wheel (`uv build`), installed it in a fresh venv with stock `pip` only, ran a real `codecheck audit` with real findings. `pip install --upgrade` confirmed working (0.1.0 → 0.1.1 test bump, reverted after). No package index is set up, so "upgrade" today means rebuilding + pointing pip at the new wheel, not `pip install --upgrade codecheck` pulling from a registry. |
| `uv pip install` (pip-compatible `uv` interface) | ✅ Verified | Same wheel, `uv venv` + `uv pip install`/`uv pip install --upgrade` — confirmed the same 0.1.0 → 0.1.1 upgrade test works identically, just faster. Not a separate packaging story, same wheel either way. |
| Ubuntu / Linux (Tier 1, packaging) | ✅ Verified | Real `ubuntu:24.04` Docker container: `uv sync`, the full 107-test suite (1.49s, faster than macOS), `codecheck audit`/`codecheck diff` with real findings, `uv tool install --editable .`, and plain `pip install` from a built wheel — all confirmed working identically to macOS. |
| Ubuntu / Linux — Tier 2 (local LLM) | ✅ Verified | Real end-to-end: Linux container pointed `local.base_url` at `http://host.docker.internal:11434` (this machine's real, already-running Ollama with a real model) — `codecheck audit` from inside the container produced a real, correct SQL-injection finding, over a real network hop from Linux to the LLM backend. |
| Ubuntu / Linux — Tier 3 (cloud LLM) | ✅ Verified (network layer) | From the same container: a real HTTPS request to the real Groq API (`httpx.get`, invalid key on purpose) correctly completed the full TLS/DNS/HTTP cycle and got a real `401 Invalid API Key` back — proves the network stack works cleanly on a bare container (missing CA certs is a common Linux-container gotcha; not an issue here). Combined with Tier 2's proof that the exact same request-building/response-parsing code path works on Linux, this covers the tier without needing to spend a real API key on it. |
| Cloud tier 429 retry-with-backoff (`post_with_retry`) | ✅ Verified | Real rate-limited Groq free-tier account (12k tokens/minute): confirmed via a process stack sample that a long-running `audit --cloud` was genuinely inside a real `time.sleep()` backoff wait (not hung), and that a full run converged from 0 usable files on the very first (pre-retry-logic) attempt to reviewing the majority of a 71-file repo unattended in one invocation. |
| `--resume-from` | ✅ Verified | Real rate-limited Groq run: pointing a retry at a prior run's own `report.json` correctly skipped the 5 files already reviewed and picked up new ones (5/68 → 9/68 cumulative) instead of re-hitting the same first few files every time, which is what a naive re-run without `--resume-from` was confirmed to do. |
| Live per-file progress (`on_progress`) | ✅ Verified | Observed real `(N/total) path: outcome` lines during a live local-LLM audit, confirming the callback fires correctly per file rather than only at the end. |
| Private-repo credential prompt | ⚠️ Partially verified | The "no interactive terminal → fail immediately, never hang" path is verified against a real inaccessible github.com repo in a non-interactive shell. The actual interactive prompt-and-retry flow (asking for a username/token at a real TTY, including the wrong-credentials/3-attempts case) is covered by unit tests with a mocked terminal and a mocked `git` call, not a live end-to-end run against a real private repo with real typed input — that needs a human at an actual terminal to confirm. |

**If you test one of the ⚠️ rows and it works (or doesn't), corrections via a
PR are genuinely wanted** — update this table with what you found, not just
the code if a fix was needed. An untested row isn't a promise it's broken,
just an honest admission nobody has confirmed it yet.

For Tier 2 (local LLM) specifically, see "Which model to run locally" in
[Architecture](Architecture.md) for hardware-tiered model suggestions — those are also a
starting point based on August 2026 benchmarks, not a verified guarantee, and
the same ask applies: update it with what you actually observed. That section
now also includes a real four-model comparison (gemma-4-12b,
llama-3-groq-8b-tool-use, qwen2.5-7b-instruct, qwen2.5-coder-7b-instruct) run
against this repo on real hardware via LM Studio, worth reading before picking
a model by name/marketing alone — the model with the best tool-calling
reliability had the worst finding quality (real fabrication, not just noise).

---

[← Documentation index](Home.md)
