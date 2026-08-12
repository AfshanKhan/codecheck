# Architecture & internals

## The shared target abstraction

Both modes funnel into the same reviewers through one concept —
`ReviewTarget` ([models.py](../src/codecheck/models.py)):

```python
@dataclass
class ReviewTarget:
    path: str
    status: str  # "added" | "modified" | "deleted" | "renamed" | "scanned"
    diff_text: str = ""
    changed_lines: set[int] | None = ...  # None = every line in scope
    old_path: str | None = None
```

- `diff` mode ([diff.py](../src/codecheck/diff.py)) builds these from git diff
  output, with `changed_lines` set to the exact line numbers touched.
- `audit` mode ([repo_scan.py](../src/codecheck/repo_scan.py)) builds these from a
  full repo file walk, with `changed_lines=None` — every reviewer and house
  check treats `None` as "no line filter, review the whole file."

This is why every tier (rules engine, house rules, cloud LLM) works identically
in both modes — they don't know or care which mode produced their input list.

## How each tier works

### Tier 1 — Rules engine (always runs, zero cost)

[`reviewers/rules_engine.py`](../src/codecheck/reviewers/rules_engine.py) fans out to
independent sub-runners, each implementing `SubRunner.is_available()` /
`SubRunner.run()`. A sub-runner that reports itself unavailable (e.g. `ruff` not
installed, or no eslint config in the repo) is skipped rather than treated as an
error — and its reason is recorded and surfaced in the report's "Skipped"
section, so an enabled-but-skipped linter isn't silently mistaken for one that
ran and found nothing.

| Sub-runner | Availability check | What it does |
|---|---|---|
| `RuffRunner` | `ruff` on `PATH` | Runs `ruff check --output-format=json -- <targets>` on `.py` targets. |
| `EslintRunner` | An eslint config file exists in the repo root (`.eslintrc*`, `eslint.config.{js,mjs,cjs}`) **and** a `PATH`-resolved `eslint` binary is found — the reviewed repo's own `node_modules/.bin/eslint` is deliberately never run (see "Security considerations"). | Runs `eslint --format=json -- <targets>` on `.js/.jsx/.ts/.tsx` targets. |
| `SemgrepRunner` | `semgrep` on `PATH` | Runs `semgrep --config=auto --metrics=off --json --quiet -- <targets>` on all non-deleted targets (language-agnostic; `--metrics=off` disables scan telemetry). |
| `HouseRulesRunner` | always available | Runs the checks in `checks/registry.py` against `.py` targets' current content. |

**Line scoping:** every Tier 1 finding is filtered through `_line_in_scope()` —
if `target.changed_lines` is a set (diff mode), only lines in it pass; if it's
`None` (audit mode), everything passes. In diff mode this is deliberate: these
linters run over the whole file, and without the filter every PR would surface a
repo's entire pre-existing lint debt instead of just what changed.

**Ruff severity mapping** (`_ruff_severity`):
- `S...` (flake8-bandit security rules) → `HIGH`
- `E9...`, `F82...` (syntax errors, undefined names) → `HIGH`
- `F...`, `B...` (pyflakes, bugbear correctness) → `MEDIUM`
- everything else → `LOW`

**Semgrep severity mapping**: `ERROR`→`HIGH`, `WARNING`→`MEDIUM`, `INFO`→`LOW`.

**Eslint severity mapping**: eslint's own `severity: 2` (error) → `MEDIUM`,
`severity: 1` (warn) → `LOW`.

### House rules (`checks/`)

Two checks registered in [`checks/registry.py`](../src/codecheck/checks/registry.py),
both AST-based (not regex) against the current file content, both accepting
`changed_lines: set[int] | None` with the same None-means-everything semantics:

- **`RULE-001`** — bare `except:` clause (`checks/no_bare_except.py`), severity `MEDIUM`.
- **`RULE-002`** — `frappe.db.sql(...)` called with an f-string, `%`-formatting,
  string `+` concatenation, or `.format()` as the query argument
  (`checks/no_sql_string_format.py`), severity `HIGH`. Parameterized calls
  (`frappe.db.sql(query, params)`) are not flagged.

Adding a new house rule: subclass `HouseCheck` in `checks/base.py`, implement
`check_file(file_path, content, changed_lines) -> list[Finding]` (handling
`changed_lines is None` yourself), add an instance to `ALL_CHECKS` in
`registry.py`. No wiring needed elsewhere.

### Tier 2 — Local LLM (opt-in, `--local`)

[`reviewers/local_llm.py`](../src/codecheck/reviewers/local_llm.py). Talks to any
local OpenAI-compatible server via `local.provider`:

| `provider` | Default `base_url` |
|---|---|
| `lm_studio` (default) | `http://localhost:1234/v1/chat/completions` |
| `ollama` | `http://localhost:11434/v1/chat/completions` |
| `openai_compatible` | **required**, set `local.base_url` yourself |

`local.base_url` always overrides the preset if set, regardless of provider. No
API key required by default, no cost, no network call beyond localhost.

It's built on the exact same shared base as the cloud tier's OpenAI-compatible
backend — see `OpenAIProtocolReviewer` below — since a local server and a free
hosted one (Groq, Mistral, ...) speak the identical wire protocol; only the
endpoint resolution differs. `is_available()` requires `local.enabled` (or
`--local`), a resolvable `base_url`, and a `local.model` (there's no sane
default model name — it has to match whatever's actually loaded in your local
server, e.g. `ollama pull qwen2.5-coder:7b` first). An optional
`local.api_key_env` covers local servers that do require auth.

#### Which model to run locally (as of August 2026)

This tier needs a model that reliably produces structured tool calls, not just
one that "does code well" — a model can write great code and still fail this
tier if it doesn't call `report_findings` properly. Confirmed real, not
theoretical: `llama3.2:1b` failed outright on Ollama; `qwen2.5-coder:7b` was
inconsistent on Ollama but failed 2/2 on `llama-server` — **the same model
name behaved differently depending on which server ran it**, so "pick a good
model" isn't sufficient advice on its own; the serving layer matters too, and
only LM Studio has been observed to handle this reliably so far (see the
per-provider accounts above). Pick by what your machine actually has, not by
general popularity:

| Your hardware | Suggested model | Why |
|---|---|---|
| ~8GB RAM/VRAM, CPU-only or entry GPU | `llama3-groq-tool-use:8b`, or Qwen3 8B | Purpose-built/native tool-calling support at a size that still runs acceptably without a GPU. |
| ~12–16GB VRAM | Qwen3-Coder 30B (Q4), or Qwen3 8B if that's too slow | Code-shaped tool use; still fits comfortably. |
| ~24GB+ VRAM | Gemma 4 27B, or Command-R 35B for multi-step tool chains | Meaningfully better reasoning at a size most single-GPU rigs can still run. |
| ~48GB+ VRAM / large unified memory (Apple Silicon 64GB+) | `llama3-groq-tool-use:70b`, or Llama 3.3 70B | Highest measured tool-calling reliability (Berkeley Function Calling Leaderboard), if you have the memory for it. |

Rough VRAM math: a 7–8B model needs ~4–8GB at Q4 quantization, 13–14B needs
~8–12GB, 30B needs ~16–24GB, 70B needs ~40GB+ — quantization and context length
shift this, so treat it as a starting point, not a guarantee.

**This list is a starting point, not a verified benchmark** — model quality
and availability shifts constantly, and our own real testing already found
that "should support tool calling" and "reliably does, through this specific
server's OpenAI-compat layer" are different claims (see the Ollama findings
below). If you run one of these (or a different model) through `--local` and
it works well — or doesn't — **please update this table via a PR** with what
you actually observed, including the provider (LM Studio vs. Ollama) and
model size, since reliability has turned out to depend on that combination,
not just the model name.

**Verified against a real running Ollama instance**, and this surfaced a real
protocol gap: Ollama's OpenAI-compat layer doesn't reliably populate
`message.tool_calls` even with `tool_choice: "required"` — the model does its
job correctly, but the serving layer sometimes serializes the tool call as JSON
text inside `message.content` instead. `llama3.2:1b` was too weak to produce
well-formed JSON there at all (it emitted malformed/prose-like text — handled
as an ordinary per-file skip, no crash). `qwen2.5-coder:7b` reliably reasoned
out correct findings, but which field they landed in varied run to run.

To handle this without violating the "never regex-scrape free text" rule,
`_extract_findings_from_content()` in
[`openai_protocol.py`](../src/codecheck/reviewers/openai_protocol.py) does a
**strict `json.loads`** of the full `content` string as a fallback when
`tool_calls` is empty — it only recovers findings if `content` parses cleanly
to exactly `{"findings": [...]}` or `{"name": "report_findings", "arguments":
{...}}` (arguments as a dict or a JSON-string). If `content` is prose, or JSON
that just happens to be malformed (we saw a real case: an unquoted bareword
value, e.g. `"name": report_findings,` instead of `"name":
"report_findings",`), it returns `None` and falls through to the ordinary
per-file skip — no guessing, no partial recovery from broken JSON. This
fallback isn't Ollama-specific in code; it applies to every
`OpenAIProtocolReviewer` (cloud OpenAI-compatible backends too), on the theory
that any server could exhibit the same misrouting.

No context-building (symbol index, embeddings) here, same as the cloud tier —
each file still gets its full content + diff sent per request.

#### llama.cpp (`llama-server`) — tested, forced tool-calling is unreliable

llama.cpp is the inference engine that (at least partly) underlies both LM
Studio and Ollama, but its own `llama-server` has an independently-written
OpenAI-compat layer, so it was tested separately rather than assumed to behave
like either — via `provider: openai_compatible` + `base_url:
http://localhost:8080/v1/chat/completions` (llama.cpp has no dedicated preset;
there's no equivalent of LM Studio/Ollama's device-management layer to name
one for).

**This testing found and fixed two real bugs that apply to every provider,
not just llama.cpp:**

1. **No `max_tokens` cap.** The OpenAI-compatible request payload never set
   one. Groq/LM Studio/Ollama all happened to have reasonable server-side
   defaults and never exposed this. `llama-server` didn't: a 7B model
   generated 6600+ tokens without ever calling the tool, at ~23 tok/s — several
   minutes of pure waste on a request that was never going to succeed. Fixed:
   `max_tokens: 4096` is now always sent.
2. **120s client timeout too short for real local inference at this speed.**
   Even capped at 4096 tokens, generation legitimately took ~3 minutes on this
   hardware. Fixed: `request_timeout_seconds` is now a config field on both
   `CloudConfig` (default `120.0`, hosted APIs are fast) and `LocalConfig`
   (default `300.0`, confirmed real local generation needs it) — raise it
   further in `config.yaml` for slower hardware.

**Correction**: earlier messages in this session claimed this ran "CPU only, no
GPU offload" — that wasn't verified, just assumed, and was very likely wrong.
Homebrew's `llama.cpp` on Apple Silicon ships with Metal GPU support enabled by
default, and `--n-gpu-layers 0` (the flag that forces CPU-only) was never
passed here. The user directly observed GPU activity during this testing,
which contradicts the CPU-only claim. The `llama-server` log was not checked
for its Metal-initialization message before `llama.cpp` was uninstalled, so
this can't be re-verified after the fact — treat "~23 tok/s" as this
machine's real observed throughput, not as evidence of which compute backend
produced it.

**What testing did *not* fix, because it isn't a `codecheck` bug**: with
`max_tokens` and the timeout both fixed, `llama-server` (Qwen2.5-Coder-7B-
Instruct-GGUF, Q4_K_M, `--jinja`) still failed to produce a proper
`tool_calls` response in 2 out of 2 tries, despite `tool_choice: "required"`.
The model correctly reasoned out the exact right finding (right CVE class,
right line, right fix) but emitted it as malformed text in `content` instead
— starting with `<{{"name": ...` (a stray `<`, doubled `{{`), then repeating
the same JSON block several times wrapped in markdown fences. This isn't
valid JSON by any reasonable definition, so `_extract_findings_from_content()`
correctly returned `None` and fell through to an ordinary per-file skip — the
system did exactly what it's supposed to do when a model produces genuinely
unusable output: decline to guess, not crash, not silently drop the real
answer without a trace. Whether a different model, quantization, or
`llama-server` chat-template flag would do better is untested — if you find a
combination that reliably works, that's exactly the kind of correction this
README's "what's verified" table is asking for.

#### LM Link: confirming which device actually runs the model (`provider: lm_studio` only)

This whole section is LM Studio-specific — Ollama and `openai_compatible` have
no equivalent multi-device concept, so `codecheck` skips this gate entirely for
them (`_confirm_local_execution()` in `cli.py` returns immediately unless
`local.provider == "lm_studio"`) rather than describing a scenario that can't
happen with those.

If you use LM Studio's [LM Link](https://lmstudio.ai/link) to point at a model
loaded on another machine, `local.base_url` stays `localhost:1234` regardless —
LM Studio resolves the model ID to whichever device has it loaded and routes
transparently. The problem: if the *same model ID* happens to be loaded both
locally and on a linked remote device, there's no guarantee which one serves a
given request, and we confirmed directly that a stale local-loaded model can
silently take priority over the remote one you actually intended to use.

Before running the local tier, `codecheck` resolves this explicitly via
[`lm_link.py`](../src/codecheck/lm_link.py) (`resolve_model_location()`), which
shells out to `lms link status --json` (does `local.model` appear in any linked
peer's `loadedModels`?) and `lms ps --json` (is it loaded locally instead?) —
LM Studio's own CLI, not a guess from resource-usage monitoring, which we
verified is unreliable (CPU%, and even the process literally named "GPU
Helper", don't reflect ML inference at all — see below).

- **Different models on local vs. remote** (`local.model` names one specific
  model — resolution is keyed on that exact ID, so which *other* models happen
  to be loaded elsewhere is irrelevant) → resolves cleanly to wherever that one
  model is loaded, no ambiguity, no prompt. Note: `lms ps --json` lists every
  model loaded anywhere on the LM Link network, not just this machine — we hit
  a real bug where a remote-only model's entry (which carries a non-null
  `deviceIdentifier`) was misread as "also loaded locally," incorrectly
  triggering the ambiguous-device flow. Fixed by checking that field; verified
  against a real setup with different models loaded on each side.
- **Confirmed remote (one device only)** → prints which device, proceeds
  automatically, no prompt.
- **Confirmed local, or undetermined** (not loaded anywhere findable, or the
  `lms` CLI isn't installed) → in an interactive terminal, prompts for
  confirmation before proceeding; in a non-interactive context (CI, scripts),
  refuses by default and tells you to pass `--force-local`.
- **Loaded on more than one device at once** (e.g. the same model ID loaded
  both locally and on a linked remote) — genuinely ambiguous, since LM Studio
  picks one silently and we confirmed it doesn't reliably favor either side.
  `codecheck` doesn't guess: pass `--device <name>` (a device name from `lms
  link status`, or the literal `local`) to pick explicitly, or `--force-local`
  to accept whatever LM Studio defaults to. In an interactive terminal with
  neither flag, it prints a numbered list of the loaded devices and prompts you
  to choose. Non-interactively with neither flag, it refuses and tells you to
  pass `--device`.
- `--device <name>` doesn't just pick for this one request — it calls `lms link
  set-preferred-device`, which changes LM Studio's own LM Link device
  preference. **This is a persistent app setting, not scoped to the current
  run** — there's no `lms` command to read or restore whatever it was set to
  before, so `codecheck` tells you explicitly when it changes it rather than
  doing so silently.
- `--force-local` (without `--device`) skips the confirmation/refusal entirely
  and proceeds without touching the device preference — use it once you've
  decided local execution (or the ambiguity) is fine.

This only degrades gracefully, never crashes — verified end to end against a
real LM Link setup (a second laptop as the remote device, confirmed via
`lms ps` flipping to `GENERATING`/`PROCESSINGPROMPT` on the expected device for
each case): a single confirmed-remote device ran automatically and produced
real findings; the same model ID loaded locally *and* remotely at once was
correctly detected as ambiguous, refused without `--device`/`--force-local`,
and `--device "<remote-device-name>"` vs. `--device local` each reliably
routed the actual generation to the requested device.

**A note on how we verified "remote" in the first place**, since we got it
wrong once: `ps -o pcpu` on LM Studio's "GPU Helper" process is *not* evidence
of anything — that process is Chromium's UI-compositing helper (every Electron
app has one), and `ps` only reports CPU%, never GPU%, regardless. The only
reliable signal is LM Studio's own state, i.e. `lms ps` / `lms link status`
showing the model's `STATUS` flip to `GENERATING`/`PROCESSINGPROMPT` on the
expected `DEVICE` for the duration of a real request — which is exactly what
`resolve_model_location()` checks.

### Tier 3 — Cloud LLM (opt-in, `--cloud`)

[`reviewers/cloud_llm.py`](../src/codecheck/reviewers/cloud_llm.py) has two backend
implementations:

- `AnthropicCloudReviewer` — Anthropic's Messages API, its own small per-file
  loop since the request/response shape doesn't match OpenAI's.
- `OpenAICompatibleCloudReviewer` — Groq, Mistral, Cerebras, OpenRouter, or a
  custom `openai_compatible` endpoint (see [Configuration](Configuration.md)).
  Built on
  [`reviewers/openai_protocol.py`](../src/codecheck/reviewers/openai_protocol.py)'s
  `OpenAIProtocolReviewer` — the shared request-building, forced-tool-call
  parsing, and per-file skip loop for *any* reviewer speaking the OpenAI
  chat-completions protocol. `LocalLLMReviewer` (Tier 2, above) is built on the
  exact same base — only `_resolved_base_url()`/`_resolved_api_key_env()` differ
  between the two.

`build_cloud_reviewer(config)` picks between the Anthropic and OpenAI-compatible
backends based on `cloud.provider`; this is what `cli.py` actually calls.

Both are gated by `is_available()`: requires `cloud.enabled` (or `--cloud`)
**and** the relevant API key env var to be set (except `openai_compatible` with
no `api_key_env`, e.g. an unauthenticated local endpoint). If either check fails,
the tier is skipped — never a silent/implicit call.

**Request shape** — one HTTP call per target file (not one call for the whole
diff or repo):

- Anthropic: `POST https://api.anthropic.com/v1/messages`, `x-api-key` header,
  `tools`/`tool_choice` pinned to a `report_findings` tool — response parsed from
  `content[].input.findings`.
- OpenAI-compatible: `POST <cloud.base_url>`, `authorization: Bearer <key>`
  header (only if an api key env is resolved), `tools`/`tool_choice` pinned to a
  `report_findings` function — response parsed from
  `choices[0].message.tool_calls[].function.arguments` (a JSON string, parsed
  with `json.loads`).

Either way, the model is forced to call the findings tool — never scraped from
prose — using the same JSON schema for
`severity/title/explanation/line_start/line_end/suggestion`, and the same fixed
system prompt: focus on logic bugs, unhandled edge cases, security issues, and
Frappe/ERPNext anti-patterns (unsafe `frappe.db.sql`, missing permission checks,
DB writes bypassing ORM hooks) — explicitly *not* style/formatting. The user
message carries the file path, status, and full current file content, plus
either the unified diff hunk (`diff` mode) or a note that this is a full-file
audit with no diff (`audit` mode).

**Per-file skip conditions** (recorded in the reviewer's `skipped_files` and
surfaced in the final report's "Skipped" section, not silently dropped):
- File status is `deleted` → skipped outright, never sent.
- File content can't be read → `"could not read file content"`.
- File exceeds `cloud.max_file_lines` → `"file too large (<n> lines > <max>)"`.
  No chunking/map-reduce — a file over the limit is skipped in full.
- The HTTP request raises `httpx.HTTPError` → error skipped for that file only;
  the rest of the review continues. The skip message includes the response body
  when there is one (`format_http_error()` in `openai_protocol.py`) — plain
  `str(e)` on an `httpx.HTTPStatusError` omits it, and the actual reason (rate
  limit details, a validation error) almost always lives there. Confirmed
  against a real 400 from Groq where the bare exception message alone gave no
  indication of the cause.
- A response comes back without the expected tool call block (`tool_use` for
  Anthropic, `tool_calls` for OpenAI-compatible) → treated as a skip, not a crash.

**Diff-scope enforcement**: the system prompt asks the model to only report
findings on lines that were actually changed, but that's advisory only —
confirmed against a real Groq request where the model reported a finding on a
line four lines away from anything in the diff. `within_diff_scope()` in
`openai_protocol.py` enforces this programmatically after the fact (same ±2
line tolerance as the aggregator's dedupe window), for both backends. In
`audit` mode (`changed_lines=None`) every line is in scope, so this never
filters anything there.

**Cloud cost cap**: before the cloud tier makes any calls, the CLI checks
`exceeds_audit_cap()` — if the number of eligible (non-deleted) target files
exceeds `cloud.audit_file_cap`, it refuses to start and exits with code `2`,
printing the file count and requiring `--force-cloud` to proceed. This now
applies to **both** `audit --cloud` and `diff --cloud`: a diff is only bounded
when you trust its author, and an attacker-controlled PR can touch arbitrarily
many files. It doesn't apply to `--local` — a local server costs nothing per
call, so `--local` on a huge repo just takes a while, it doesn't need a safety rail.

**Finding IDs**: sequential `CLOUD-001`, `CLOUD-002`, ... across the whole run
(not per file).

**Verified against a real Groq account** (`llama-3.3-70b-versatile`, free
API key): both `audit --cloud` and `diff --cloud` correctly identified a real
SQL injection vulnerability, with accurate severity/line/explanation, in under
1.5 seconds per run. This testing is what surfaced the diff-scope-enforcement
gap and the missing-response-body gap above, plus an unrelated real crash
(below) — all fixed and covered by regression tests, not just claimed.

## Aggregator — merge and dedupe

[`aggregator.py`](../src/codecheck/aggregator.py) takes `{tier_name: [Finding, ...]}`
and:

1. Flattens all findings into one list.
2. Two findings are considered the same underlying issue if: same `file`, their
   line ranges overlap (within a ±2 line window), **and** their titles are
   similar (`difflib.SequenceMatcher` ratio ≥ `0.6` on lowercased titles).
3. When two findings merge, the one from the higher-priority tier is kept as
   primary (`cloud_llm` > `local_llm` > `rules`), the other's `source:check_id` is
   appended to `raw["also_flagged_by"]`, and severity becomes the max of the two.
4. Final list is sorted: severity descending, then file, then line number.

**Known limitation, confirmed by testing:** this only catches duplicates when the
*wording* is close. It does **not** merge cases where two tools flag the exact
same line for the exact same underlying issue but describe it very differently —
concretely, ruff's `E722: Do not use bare `except`` and our own `RULE-001: Bare
except clause` on the same line do **not** get merged (ratio ≈ 0.46, below
threshold) and both show up in the report.

## Reporters

- [`reporters/console.py`](../src/codecheck/reporters/console.py) — one `rich` panel
  per file, severity-colored, plus a summary line and a "Skipped" section listing
  every skip reason gathered from any tier.
- [`reporters/json_report.py`](../src/codecheck/reporters/json_report.py) —
  `ReviewReport.to_dict()` dumped as-is, including the `mode` field
  (`"diff"` or `"audit"`); the CI/tooling-consumable artifact.
- [`reporters/markdown_report.py`](../src/codecheck/reporters/markdown_report.py) —
  one `##` heading + table per file, severity shown as emoji, meant for pasting
  into a PR description or Slack.

Both file reporters are always written on every run — there's no flag to
suppress them.

## Project layout

```
src/codecheck/
├── cli.py                  # typer entrypoint: `diff` and `audit` subcommands, shared pipeline helper
├── config.py                # pydantic Config/RulesConfig/CloudConfig/LocalConfig/ThresholdsConfig + load_config()
├── models.py                # Finding, Severity, ReviewTarget, ReviewReport
├── diff.py                  # git diff extraction -> ReviewTarget (staged / base-ref merge-base)
├── repo_scan.py              # whole-repo file walk -> ReviewTarget (audit mode)
├── github_source.py           # --pr (isolated git worktree fetch) and --repo-url (clone) support
├── lm_link.py                  # resolves which device (local vs LM Link remote) serves local.model
├── aggregator.py              # cross-tier merge + dedupe
├── reviewers/
│   ├── base.py                # Reviewer ABC (is_available / review)
│   ├── rules_engine.py        # Tier 1: SubRunner ABC + Ruff/Eslint/Semgrep/HouseRules runners
│   ├── openai_protocol.py     # shared OpenAI chat-completions request/parsing/loop, used by Tiers 2 and 3
│   ├── local_llm.py           # Tier 2: local OpenAI-compatible server (LM Studio, Ollama, ...), no cost
│   └── cloud_llm.py           # Tier 3: Anthropic + OpenAI-compatible (Groq/Mistral/Cerebras/OpenRouter/custom) backends, sync httpx, tool-forced JSON, audit cap
├── checks/
│   ├── base.py                 # HouseCheck ABC
│   ├── no_bare_except.py       # RULE-001
│   ├── no_sql_string_format.py # RULE-002
│   └── registry.py             # ALL_CHECKS list, auto-picked-up by HouseRulesRunner
└── reporters/
    ├── console.py, json_report.py, markdown_report.py
tests/                        # pytest, 108 tests total, including CLI-level tests via typer.testing.CliRunner
```

## Running tests

```bash
uv run pytest tests/ -q
```

---

[← Documentation index](Home.md)
