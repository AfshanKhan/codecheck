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
| `HouseRulesRunner` | always available | Runs the checks in `checks/registry.py` against `.py`/`.js`/`.json` targets' current content. |

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

32 checks registered in [`checks/registry.py`](../src/codecheck/checks/registry.py),
all accepting `changed_lines: set[int] | None` with the same
None-means-everything semantics. `HouseRulesRunner` (`reviewers/rules_engine.py`)
passes `.py`, `.js`, and `.json` targets — each check filters to its own
extension/path pattern internally, so `check_file()` is a no-op on a file
type or path shape it doesn't handle.

**Python checks (AST-based, via the `ast` module) — `RULE-001` through `RULE-009`:**

- **`RULE-001`** — bare `except:` clause (`checks/no_bare_except.py`), severity `MEDIUM`.
- **`RULE-002`** — `frappe.db.sql(...)` called with an f-string, `%`-formatting,
  string `+` concatenation, or `.format()` as the query argument
  (`checks/no_sql_string_format.py`), severity `HIGH`, downgraded to `MEDIUM`
  if `frappe.db.escape()` appears anywhere in the call. Parameterized calls
  (`frappe.db.sql(query, params)`) are not flagged.
- **`RULE-003`** — `@frappe.whitelist()` method whose body never (transitively)
  calls something matching `has_permission`/`check_permission` or raises a
  `Permission*` exception (`checks/whitelist_permission_check.py`), severity
  `HIGH`. Skips `allow_guest=True` endpoints. Resolution is call-graph-aware,
  not a flat `ast.walk()` of the whole function: a permission check inside a
  nested helper only counts if that helper is actually reachable by a call
  from the whitelisted function (directly, or transitively through other
  nested helpers) — an unused, never-invoked nested helper doesn't count.
  Greptile caught three bugs getting here: first a plain `ast.walk()` matched
  even a dead/uncalled nested helper; the fix for that (stop descending into
  any nested scope) then broke the opposite, valid case — a permission check
  in a helper that IS called; fixing *that* with a flat, depth-independent
  name→def map then got name resolution wrong when two helpers share a name
  at different nesting depths (whichever definition appeared last in
  traversal order won, not necessarily the one actually reachable from a
  given call site). The current version resolves both correctly: reachability
  via the real call graph (which helper is actually invoked, directly or
  transitively), and each call's target via the real lexical scope chain
  (walking outward from the call site's own scope, same as a Python closure
  would) — so a shadowed name resolves to the nearest enclosing definition,
  not an arbitrary same-named one elsewhere in the method.
- **`RULE-004`** — a Frappe DB/document-fetch call (`get_doc`, `get_all`,
  `db.sql`, etc.) made inside a loop body (`checks/n_plus_one_query.py`),
  severity `MEDIUM`.
- **`RULE-005`** — a manual `frappe.db.commit()`/`db.commit()` call
  (`checks/no_manual_commit.py`), severity `MEDIUM`.
- **`RULE-006`** — `frappe.throw()`/`msgprint()` with a raw (non-`_()`-wrapped)
  message (`checks/missing_translation.py`), severity `LOW`.
- **`RULE-007`** — a leftover `print()` call (`checks/leftover_print.py`),
  severity `LOW`.
- **`RULE-008`** — a typed `except SomeError:` whose body is just `pass` (or a
  docstring stub) (`checks/silent_exception.py`), severity `MEDIUM`. Distinct
  from `RULE-001`, which only matches a fully bare `except:`.
- **`RULE-009`** — a variable named like a secret (password/token/key/etc.)
  assigned a non-placeholder string literal (`checks/hardcoded_credential.py`),
  severity `HIGH`.
- **`RULE-018`** — a `def`/`async def` longer than 50 lines
  (`checks/method_too_long.py`), severity `LOW`. Added after a live comparison
  against `frappe-pr-reviewer` on a real PR showed it flagging 5 long methods
  `codecheck` had no equivalent for at the time. Diff scope is checked against
  the function's whole line range, not just its `def` line — a diff touching
  only the body of an existing long function still counts as touching it
  (Greptile catch; same shape as the `RULE-015` fix below).

**JS checks (regex/line-based, no JS AST parser dependency) — `RULE-010` through `RULE-016`:**

- **`RULE-010`** — hardcoded `<input>`/`<button>` HTML in a client script
  (`checks/js_hardcoded_html.py`), severity `MEDIUM`.
- **`RULE-011`** — an inline `style=` attribute (`checks/js_inline_style.py`),
  severity `LOW`.
- **`RULE-012`** — a leftover `console.log()`/`console.debug()`
  (`checks/js_console_debugger.py`), severity `LOW`.
- **`RULE-013`** — a leftover `debugger;` statement
  (`checks/js_console_debugger.py`), severity `HIGH`.
- **`RULE-014`** — raw jQuery DOM manipulation (`$(...)`/`jQuery(...)`),
  excluding `$wrapper`/`frm.fields_dict` (`checks/js_jquery_dom.py`), severity
  `LOW`.
- **`RULE-015`** — a `frappe.call()` with no `error:`/`callback:`/`.catch()`/
  `freeze: true` signal within the following ~12 lines
  (`checks/js_frappe_call_error_handling.py`), severity `LOW`. Matches
  `frappe` and `.call(` across a line break, not just on one line — a live
  comparison against `frappe-pr-reviewer` on a real PR found a Prettier-
  formatted `frappe\n\t.call({` split across two lines that the original
  same-line-only regex missed entirely (zero findings in a file that should
  have had two). Diff scope is checked against every line the match spans,
  not just the line it starts on — a diff that only touches the `.call(`
  line of an existing `frappe\n.call(` pair (the `frappe` line itself
  unchanged) still counts as touching it (Greptile catch).
- **`RULE-016`** — a JS variable named like a secret assigned a hardcoded
  string (`checks/js_hardcoded_credential.py`), severity `HIGH`.

`RULE-003` through `RULE-016` were ported from a sibling project
(`frappe-pr-reviewer`)'s deterministic Python/JS analyzers, with one fix along
the way: its permission check only matched the substring `has_permission`,
missing the equally-valid `check_permission()` pattern — confirmed as a real
false positive against a live PR. `RULE-003` here matches both.

**`RULE-020` through `RULE-034`** — a second batch, independently designed
(not ported) to cover a broader set of Frappe app-review concerns than
`RULE-001`-`RULE-018` did on their own:

- **`RULE-020`** — `hooks.py` contains something other than a plain
  assignment/import at module level (`checks/hooks_structure.py`), severity
  `LOW`. `hooks.py` is read by Frappe at app boot and is expected to stay
  purely declarative.
- **`RULE-021`** — `hooks.py` sets a non-empty `override_doctype_class`
  (`checks/hooks_structure.py`), severity `MEDIUM` — a sharp, easy-to-forget
  edge that can break silently on a framework upgrade.
- **`RULE-022`** — a DocType definition file
  (`.../doctype/<name>/<name>.json`) that isn't valid JSON
  (`checks/doctype_schema.py`), severity `MEDIUM` — breaks `bench migrate`.
- **`RULE-023`** — a DocType field's `default` value looks like a JSON blob
  (`checks/doctype_schema.py`), severity `LOW` — usually means the field
  should be a child table instead of plain text.
- **`RULE-024`** — an outbound HTTP call (`requests.*`,
  `urllib.request.urlopen`) inside a document lifecycle hook (`validate`,
  `before_save`, `on_submit`, ...) (`checks/blocking_call_in_doc_event.py`),
  severity `MEDIUM`.
- **`RULE-025`** — synchronous PDF generation (`frappe.get_pdf`/`get_print`,
  `pdf.make`) inside the same set of lifecycle hooks
  (`checks/blocking_call_in_doc_event.py`), severity `LOW`. `RULE-024` and
  `RULE-025` share one AST visitor (own-scope-only, same reachability
  approach as `RULE-003`) but were deliberately given *dissimilar* titles —
  a first version with near-identical boilerplate titles ("... inside a
  document lifecycle hook") on two adjacent lines tripped the aggregator's
  cross-check dedup heuristic (line-window + title-similarity, see
  "Deduplication" below) and silently dropped one of two genuinely distinct
  findings.
- **`RULE-026`** — a `.save()` call inside a loop
  (`checks/save_in_loop.py`), severity `LOW` — the write-side equivalent of
  `RULE-004`'s read-side N+1 check.
- **`RULE-027`** — a file using both `frappe.call` and `.then(` with no
  `async` keyword anywhere (`checks/js_async_await_suggestion.py`), severity
  `LOW` — a suggestion, not a correctness issue.
- **`RULE-028`** — `document.getElementById`/`querySelector`/`.innerHTML`
  (`checks/js_direct_dom.py`), severity `LOW` — the vanilla-JS-DOM
  equivalent of `RULE-014`'s jQuery-specific check.
- **`RULE-029`** — a client script longer than 200 lines
  (`checks/js_client_script_length.py`), severity `LOW`.
- **`RULE-030`**/**`RULE-031`** — a comment that parses as real code rather
  than prose (`checks/commented_code.py`), severity `LOW` each. `RULE-030`
  (Python) tries to `ast.parse()` the comment's text; `RULE-031` (JS, no
  parser available) falls back to a smaller set of "looks like code"
  regexes on single-line `//` comments only, to keep the false-positive rate
  down.
- **`RULE-032`** — a numeric literal (other than `0`/`1`/`-1`/`2`/`100`) used
  directly in a calculation or comparison instead of a named constant
  (`checks/magic_number.py`), severity `LOW`.
- **`RULE-033`** — a function/method with no docstring
  (`checks/missing_docstring.py`), severity `INFO`. Skips
  private/dunder names (`_helper`, `__init__`) to keep volume down — not
  present in whatever this was inspired by, added here deliberately.
- **`RULE-034`** — a function parameter with a boolean default value
  (`checks/boolean_flag_param.py`), severity `INFO`.

Adding a new **built-in** house rule: subclass `HouseCheck` in `checks/base.py`,
implement `check_file(file_path, content, changed_lines) -> list[Finding]`
(handling `changed_lines is None` yourself, and filtering to your own file
extension(s)), add an instance to `ALL_CHECKS` in `registry.py`. If it's a new
extension not already passed to `HouseRulesRunner`, extend the suffix tuple in
`HouseRulesRunner.run()` (`reviewers/rules_engine.py`). No wiring needed
elsewhere.

A **project-specific** check doesn't require touching `codecheck`'s own source
at all: `rules.extra_checks` in config.yaml takes a list of dotted
`"module.path:ClassName"` strings, dynamically imported and instantiated by
`load_extra_checks()` in `checks/registry.py`, then passed into
`HouseRulesRunner(extra_checks=...)` where they run in the exact same per-file
loop as the built-ins — a loaded check is indistinguishable from a built-in
one once running. A path that fails to import, isn't a `HouseCheck` subclass,
or can't be instantiated with no arguments is caught and reported as a
`("house_rules.extra_checks", error)` entry in `RulesEngineReviewer.skipped_runners`
(surfaced the same way a missing linter binary is) rather than raised — one
bad entry in the list doesn't take the built-in checks down with it. See
"Adding project-specific checks" in [Configuration](Configuration.md).

`RulesEngineReviewer.review()` also applies `rules.disabled_checks` as a final
post-filter over every sub-runner's combined output — not just house rules,
any `check_id` (`RUFF-F401`, `RULE-009`, ...) can be suppressed this way,
without disabling the sub-runner that produces it.

### Test-coverage check (`TestCoverageRunner`, `RULE-017`)

A different shape from the rest of Tier 1: every other sub-runner judges one
file at a time, but "did this change add a test" is a judgment about the
*whole diff* — so `TestCoverageRunner` (`reviewers/rules_engine.py`) is a
`SubRunner`, not a `HouseCheck`, and looks at the full `targets` list at once.
It's a no-op in `audit` mode (`changed_lines is None` for every target there —
there's no single "this change" to judge test coverage against, only a whole
repo, which is a different and much noisier claim).

Logic, ported from a sibling project (`pr_probe`, an org-wide PR-metrics/
compliance tool, not a code reviewer — but its `PRAnalyzer.check_tests()`
heuristic for "does this PR look like it added a real test" turned out to be
exactly as useful for a single diff):

1. Collect changed `.py`/`.js`/`.jsx`/`.ts`/`.tsx` files that aren't
   themselves test files and aren't deleted. If none, or their combined
   added-line count is under 5, skip — too small to reasonably expect a test.
   (`.jsx`/`.ts`/`.tsx` were added after Greptile pointed out the original
   extension list silently excluded them, even though the eslint sub-runner
   already covers them.)
2. "Is a test file" (`_is_test_path`) matches real test-file conventions, not
   a bare `"test" in path` substring — that substring check originally
   misclassified ordinary application files like `contest.tsx`/`latest.ts` as
   tests (a real Greptile catch). It matches: a bare `test.py`, a
   `test_*.py`/`*_test.py` module, a bare `test.<ext>`, a `.test.`/`.spec.`
   JS/TS file, a `*_test.<ext>` JS/TS file, or any path under a
   `tests/`/`__tests__/` directory. (The bare `test.<ext>` and JS/TS
   `*_test.<ext>` forms were a second, later Greptile catch — the first regex
   version rejected `test.py`/`test.tsx`/`foo_test.js` outright, even though
   they're common real test filenames.) A matched test file only counts as
   real coverage if its diff contains an actual test declaration (`def test_`,
   `test(`, `it(`, `describe(`); a `pass`-only stub under 15 added lines is
   filtered out as boilerplate, same as the source heuristic. Falling back to
   "more than 15 added lines" or "an existing test file was modified" if
   there's no clearer signal.
3. If app code changed substantially and no test file passes that bar, emit
   one `RULE-017` finding (severity `LOW`) anchored on the largest changed
   app file — not one finding per file, since this is a single judgment about
   the whole diff.

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
| ~8GB RAM/VRAM, CPU-only or entry GPU | `qwen2.5-7b-instruct` (verified — see results below), or Qwen3 8B | Real testing found the "tool-use-optimized" `llama3-groq-tool-use:8b` calls the tool more reliably but fabricates findings; a general-purpose 7B was more trustworthy despite slightly lower completion rate. See "Real results" below before picking this one on reputation alone. |
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

**Real results, M1 Pro / 16GB unified memory, LM Studio, MLX quantization
(August 2026)** — a real `codecheck audit --local` of this repo (71 files)
against four models, measuring both how many files actually got a usable
response and whether the findings were trustworthy:

| Model | Files succeeded | Findings quality |
|---|---|---|
| `google/gemma-4-12b` (4bit) | 19/71 (27%) | Both findings low-value, but no fabrication. |
| `llama-3-groq-8b-tool-use` (4bit, MLX) | **67/71 (94%)** | **Worst quality of the four** — fabricated content outright: claimed a function (`get_users`) existed and accessed a database in a file that is completely empty. Also reflexively attached "direct DB writes bypassing ORM validation" boilerplate to files with nothing to do with databases, including `CODE_OF_CONDUCT.md`. |
| `qwen2.5-7b-instruct` (4bit, MLX) | 66/71 (93%) | High reliability, no fabrication found — false positives were misreadings of real content (flagging our own AST-detector logic as if it were the vulnerable pattern it detects, and re-flagging risks we'd already documented intentionally in `docs/Security.md` as if newly discovered), not invented content. |
| `qwen2.5-coder-7b-instruct` (4bit, MLX) | 40/71 (56%) | Same false-positive pattern as the Instruct model above (flagged its own detector logic). Notably: this exact model failed forced tool-calling 2/2 via `llama-server` (see below) but works fine via LM Studio — confirms that failure was `llama-server`'s harness, not the model. |

**Conclusion: reliable tool-calling and trustworthy output are not the same
thing, and optimizing for one doesn't get you the other.** The model
literally named and fine-tuned for "tool use" had the highest call-success
rate *and* the worst hallucination rate of the four — for a security-review
tool, a model that reliably calls the function but invents vulnerabilities is
worse than one that fails to call it at all, since a failure is visibly a
skip in the report while a fabrication looks like a real finding. Based on
this, `qwen2.5-7b-instruct` is the better default recommendation for this
hardware tier over `llama3-groq-tool-use:8b`, despite the latter's higher raw
completion rate — pending someone else's real results confirming or
contradicting this on different hardware/repos.

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
- A response's JSON is malformed, isn't an object, or a `findings` array contains
  a non-object element → skipped with a specific reason rather than raising —
  confirmed necessary against real malformed/edge-case responses from both
  backends; see `post_with_retry()`'s callers in `openai_protocol.py` and
  `cloud_llm.py` for the exact guard clauses.

**429 (rate limited) is handled differently from other HTTP errors** — it
doesn't fall straight to a skip. `post_with_retry()` in `openai_protocol.py`
(shared by both backends) retries the same request in place: honors the
server's `Retry-After` header when present, exponential backoff otherwise, up
to 5 attempts, before finally giving up and treating it as an ordinary skip.
This exists because real testing against a rate-limited Groq free-tier account
(12k tokens/minute) showed that without it, most of a repo's files were left
unreviewed after a single `audit --cloud` pass — and simply re-running the
whole command didn't help either, since targets are processed in a fixed
order, so a fresh retry just re-hit the same first few files and stalled at
the same point every time. `--resume-from <prior report.json>` (see
[CLI Reference](CLI-Reference.md)) is the fallback for a run that still
doesn't converge in one invocation — it skips any file a tier already
succeeded on in a prior run and only retries what was actually skipped.

Since a rate-limited run backed by retries can take a long time with nothing
printed to the console otherwise, `review()` accepts an optional
`on_progress(path, outcome)` callback (both backends, and the shared Tier 2
base), called after each file; `cli.py` wires it to print live per-file
progress instead of leaving a single spinner up for the whole tier.

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
   similar (`difflib.SequenceMatcher` ratio ≥ `0.6` on lowercased titles) --
   **unless they share the same `check_id` and `source`**, in which case they
   only merge if they're at the exact same line range, not just an overlapping
   one. That carve-out exists because the ±2-line window is meant to catch the
   SAME issue reported by two DIFFERENT tools/checks at slightly different
   line numbers (see the ruff/RULE-001 example below); applied between two
   findings from the identical check, it does the opposite of what's wanted --
   confirmed as a real bug via a live comparison against `frappe-pr-reviewer`
   on a real PR, where three separate `print()` calls two and three lines
   apart in one file (RULE-007 firing three times) were silently collapsed
   into two findings, permanently losing the third.
3. When two findings merge, the one from the higher-priority tier is kept as
   primary (`cloud_llm` > `local_llm` > `rules`), the other's `source:check_id` is
   appended to `raw["also_flagged_by"]`, and severity becomes the max of the two.
4. Final list is sorted: severity descending, then file, then line number.

**Known limitation, confirmed by testing:** this only catches cross-check
duplicates when the *wording* is close. It does **not** merge cases where two
tools flag the exact same line for the exact same underlying issue but
describe it very differently — concretely, ruff's `E722: Do not use bare
`except`` and our own `RULE-001: Bare except clause` on the same line do
**not** get merged (ratio ≈ 0.46, below threshold) and both show up in the
report.

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
- [`reporters/docx_report.py`](../src/codecheck/reporters/docx_report.py) —
  same content as the markdown reporter (summary table + one findings table
  per file + skipped section), rendered as a Word document via `python-docx`
  (a core dependency, not optional — pure-Python/lxml, no BYO-tool story like
  ruff/eslint/semgrep) for sharing with someone who'd rather open Word than a
  `.md` file.
- [`reporters/xlsx_report.py`](../src/codecheck/reporters/xlsx_report.py) —
  a "Findings" sheet (one row per finding, header-row AutoFilter and a frozen
  header already set) and a "Summary" sheet (counts by severity and by check
  ID), via `openpyxl` (also a core dependency). Built for filtering/sorting
  in a spreadsheet rather than reading top to bottom — the gap this fills
  relative to the other three, all read-only/scroll formats. Every cell that
  can hold text derived from the reviewed repo itself (a file path, or a
  finding's title/explanation/suggestion) is Text-formatted with a leading
  `=`/`+`/`-`/`@` neutralized (`_sanitize()`/`_write_text_cell()`) — Excel
  evaluates a cell starting with one of those as a formula when the file is
  opened otherwise, and a `--repo-url`/`--pr` review's findings can contain
  text derived from a repo you don't control.

All four file reporters are always written on every run — there's no flag to
suppress them. `cli._finish()` builds a shared basename for all four via
`_report_basename()` — see "Report filenames" in [CLI Reference](CLI-Reference.md)
for the exact naming convention (`<repo>_pr<N>_<timestamp>` for a `--pr` run,
`<repo>_<mode>_<timestamp>` otherwise — never both suffixes at once, since the
previous static `report.json`/`report.md` silently overwrote the prior run's
report on every invocation). `cli._write_reports()` (the claim-then-write
logic shared by `_finish` and `render`) is what actually performs the write —
see "Re-rendering a report" below.

### Making a report readable to someone who didn't run it

[`reporters/glossary.py`](../src/codecheck/reporters/glossary.py) is the one
place that owns two things every human-facing reporter (console/markdown/
docx/xlsx — not JSON, see below) renders into its own format, so the wording
never drifts out of sync between them:

- **`format_ist(dt)`** — every `generated_at` is always produced internally
  as `datetime.now(timezone.utc)`; this converts it to `"27 Aug 2026, 12:16
  PM IST"` for display. JSON is the one exception: it keeps the raw UTC
  ISO-8601 string untouched, since `ReviewReport.from_dict()` parses that
  field back via `datetime.fromisoformat()` for `render`/`compare`/
  `--resume-from` — JSON is documented as "for other tools to read," and
  swapping its timestamp format would silently break every consumer of it.
- **`tier_description(tier)`** / **`source_description(source)`** — a
  first-time reader has no way to already know what `tiers_run: rules` means,
  or what the `(house)`/`(ruff)`/... tag after a check ID refers to. Each
  human-facing reporter renders a short "what does this mean" line/section
  using these, listing only the sources actually present in `report.findings`
  (not every source codecheck knows about, most of which won't be relevant to
  a given run).

`report.repo_path` is the repo's own remote URL (`--repo-url`, or the URL
`--pr` was given as) whenever one was actually passed — not the local temp
clone's directory (`codecheck-clone-xxxxx`), which means nothing to a report
reader and used to be what every report showed regardless of how the repo was
sourced. Only a genuinely local run (`--repo-path`, or `--pr <number>`
against an already-cloned local checkout) shows a local filesystem path.
`redact.py`'s `_is_url_like()` guards `--redact` accordingly — a public
remote URL isn't a local path with a username to scrub, so `--redact` leaves
it exactly as-is instead of replacing it with the `<local repo>` placeholder
like a genuine local path.

## Redaction, re-rendering, and comparing reports

Three small, independent modules sit on top of `ReviewReport` for the
`--redact` flag and the `render`/`compare` commands — none of them touch the
review pipeline itself, they all operate on a `ReviewReport` that's already
been produced (or reloaded).

- [`redact.py`](../src/codecheck/redact.py) — `redact_report(report)` returns
  a new `ReviewReport` (never mutates the input) with `repo_path` replaced by
  a placeholder that keeps just the repo's own directory name, and any
  absolute-path fragment scrubbed out of `skipped` entries via a regex anchored
  to not match the internal `/` of an ordinary repo-relative path like
  `app/api.py` (`(?<![\w/])(?:/[\w.\-]+)+/?`). Findings themselves are left
  alone — file paths there are already repo-relative or generic. `cli._finish()`
  applies this to the report *before* writing it to disk when `--redact` is
  passed, but not to what's printed to the terminal for the run you're
  actively watching.
- [`report_diff.py`](../src/codecheck/report_diff.py) — `diff_reports(old,
  new)` returns `(added, resolved)`: findings whose identity
  `(file, check_id, line_start)` appears in `new` but not `old`, and vice
  versa. Line number alone (not an exact range or content match) is
  deliberate — unrelated lines shifting around a finding shouldn't make it
  look both resolved and newly introduced. Powers `codecheck compare`.
- `models.py`'s `Finding.from_dict()`/`ReviewReport.from_dict()` are the
  inverse of the existing `to_dict()` methods, added so `render`/`compare` can
  reconstruct a full `ReviewReport` from a prior run's JSON. Deliberately
  independent from `resume.py`'s own dict-to-`Finding` parsing (which
  reconstructs a finding *in the context of a specific tier being resumed*,
  a different, narrower job) — these are general-purpose deserializers,
  tolerant of a missing/malformed field (dropping just that finding, or
  falling back to a sane default) rather than raising, since they may be
  reading a report from an older `codecheck` version or one edited by hand.

`cli.render()` reads a `.json` report back via `ReviewReport.from_dict()`,
optionally redacts it, and calls the same `_write_reports()` that `_finish()`
uses for a live run — so a re-rendered report gets the identical
collision-safe, all-or-nothing write behavior, just skipping the "run every
check again" part entirely. `cli.compare()` loads two reports the same way,
calls `diff_reports()`, prints both lists, and exits `1` if any *added*
finding is at or above the resolved fail threshold — the same threshold
logic `_finish()` uses, just checked against `added` instead of `report.findings`.

## Fix suggestions (`--suggest-fixes`)

[`suggest.py`](../src/codecheck/suggest.py)'s `generate_suggestions()` fills
in `Finding.suggestion` in place on a bounded subset of the run's findings —
sorted by severity (`-f.severity.rank`) and capped at `suggestions.max_per_run`
(default `5`), excluding any finding that already has a `.suggestion` or whose
`check_id` is in `suggestions.exclude_checks`, or whose file isn't in the
current target set. `cli._maybe_suggest_fixes()` (called from both `diff()`
and `audit()`, after `aggregate()` but before the `ReviewReport` is built)
picks which already-configured reviewer to reuse — `build_cloud_reviewer(cfg.cloud)`
if `cloud.enabled` and available, else `LocalLLMReviewer(cfg.local)` if
`local.enabled` and available, else the whole pass is skipped with a
`skipped` entry rather than failing the run.

Deliberately reuses an `OpenAIProtocolReviewer` subclass instance purely for
its already-built `_get_client()`/`_resolved_base_url()`/`config.model` (the
exact same auth/timeout/base-URL resolution the review tiers themselves use),
not its `review()` method — the request this module sends is a different,
narrower shape: one specific finding's title/explanation/line plus the file
content, asking for a short fix rather than an open-ended list of new
findings, and no `tools`/`tool_choice` schema is forced, since the response
here is meant to be read as-is (a suggestion is inherently prose, so there's
no structured field to parse out of it — unlike a findings list, taking the
raw response text *is* the intended output, not a "scrape" of it). A
per-finding HTTP failure is recorded as a `skipped` entry and the pass moves
on to the next finding, never raised — reusing the same `post_with_retry` /
`format_http_error` helpers the review tiers already use for that.

### Uncovered `.env` file check (`SecretsInRepoRunner`, `RULE-035`)

Another repo-wide `SubRunner`, same shape as `TestCoverageRunner`: it needs
to see the whole tree at once (every `.env` under `repo_path`, plus every
`.gitignore` that could cover one), which a `HouseCheck` (one file's content
at a time) can't provide. `checks/secrets_in_repo.py` duck-types the
`SubRunner` interface instead of importing the ABC from `rules_engine.py`,
for the same circular-import reason `RULE-019`'s runner does (that module
imports this one to wire the runner in). Gated by `rules.secrets_scan` in
config.yaml (default `True`), same pattern as `rules.test_coverage`.

Deliberately a presence check, not a `git ls-files`-backed tracked-status
check: it flags any `.env` file present in the checkout and not matched by
a `.gitignore` (checking every directory from the repo root down to that
`.env`'s own directory, not just the root — a nested app-level `.gitignore`
counts too), regardless of whether it's actually been committed yet.
Catching one before `.gitignore` covers it is at least as valuable as
catching one after the fact, and it avoids depending on git state a plain
filesystem scan doesn't need. Doesn't try to detect a secret *inside* file
content beyond that (`RULE-009`/`RULE-016` already do that, wherever a
hardcoded-looking literal appears in reviewed source).

## Live Frappe site verification (`--frappe-db-config`, RULE-019)

Every other check in `codecheck` is purely static — a `HouseCheck` only ever
sees one file's content, never a database or the network. RULE-019 is
different by necessity: judging whether `frappe.db.get_value('Sales Order',
name, 'customre')` (a typo) is wrong requires knowing what fields actually
exist on `Sales Order` *on a specific site*, something no amount of source
reading can tell you — the DocType's real schema lives in that site's
database, shaped by both the base app and whatever Custom Fields were added
through the UI.

That's why RULE-019 isn't a `HouseCheck` in `checks/registry.py` alongside
the other rules — the `HouseCheck.check_file(file_path, content,
changed_lines)` signature has no way to hand it a database connection.
Instead, [`checks/frappe_db_field_check.py`](../src/codecheck/checks/frappe_db_field_check.py)'s
`FrappeDbFieldCheckRunner` is its own `SubRunner`, constructed with a
`FrappeDbConnection` and wired into `HouseRulesRunner`'s sibling runner list
by `RulesEngineReviewer.__init__` only when `--frappe-db-config` produced a
working connection. It deliberately duck-types the `SubRunner` interface
(`is_available`/`run`/`name`) instead of importing the ABC from
`rules_engine.py` — that module already imports *this* one to wire the
runner in, so importing back would be circular; nothing in
`RulesEngineReviewer.review()` ever does an `isinstance` check, only calls
the three methods, so duck-typing is sufficient.

[`frappe_db.py`](../src/codecheck/frappe_db.py)'s `FrappeDbConnection.connect(site_config_path)`
reads `db_name`/`db_password`/`db_type`/`db_host`/`db_port` out of a real
`site_config.json` (the same file every Frappe bench already has one of per
site) and opens a `pymysql` connection using Frappe's own convention that the
DB username equals `db_name` itself. `doctype_fields(doctype)` is the only
query surface: a hardcoded, parameterized `SELECT` against `tabDocType` (does
the DocType exist at all — a typo'd *DocType* name is a different problem
from a typo'd field name, so an unknown DocType returns `None` and the
runner treats that as "nothing to judge," not "every field is missing"),
`tabDocField` (standard fields), and `` `tabCustom Field` `` (fields added
through the UI, not in source) — unioned with a small hardcoded
`META_FIELDS` set (`name`, `owner`, `modified`, `parent`, ...) that Frappe
attaches to every DocType automatically and would otherwise all read as
false positives. Results are cached per-doctype for the run's lifetime,
since the same DocType is typically referenced from many files. The
connection is read-only in practice (every query is a fixed `SELECT`
string with parameterized values — there is no code path that assembles SQL
from file content or accepts arbitrary queries).

`cli._connect_frappe_db()` enforces the one guardrail this feature needs:
refusing to combine `--frappe-db-config` with `--repo-url`/`--pr`. Querying a
live database using doctype/field names lifted from code you don't control
and trust (an external PR, a cloned URL) is a materially different risk than
pointing it at your own local bench's checkout of your own app, so
`codecheck` refuses the combination outright rather than leaving it as an
easy-to-miss footgun. A missing `pymysql` install, an unreadable/invalid
`site_config.json`, or a database `codecheck` can't reach are all treated as
a graceful skip (`FrappeDbUnavailable`, recorded under `skipped`) — never a
reason to abort the rest of the run.

## Project layout

```
src/codecheck/
├── cli.py                  # typer entrypoint: `diff`/`audit`/`render`/`compare` subcommands, shared pipeline helper
├── config.py                # pydantic Config/RulesConfig/CloudConfig/LocalConfig/ThresholdsConfig/SuggestionsConfig + load_config()
├── models.py                # Finding, Severity, ReviewTarget, ReviewReport (+ from_dict()/to_dict() on the last two)
├── diff.py                  # git diff extraction -> ReviewTarget (staged / base-ref merge-base)
├── repo_scan.py              # whole-repo file walk -> ReviewTarget (audit mode)
├── github_source.py           # --pr (isolated git worktree fetch) and --repo-url (clone) support
├── lm_link.py                  # resolves which device (local vs LM Link remote) serves local.model
├── aggregator.py              # cross-tier merge + dedupe
├── redact.py                   # redact_report() -- scrubs repo_path/absolute paths for --redact
├── report_diff.py              # diff_reports() -- added/resolved findings between two reports, for `compare`
├── suggest.py                   # generate_suggestions() -- the --suggest-fixes second pass
├── reviewers/
│   ├── base.py                # Reviewer ABC (is_available / review)
│   ├── rules_engine.py        # Tier 1: SubRunner ABC + Ruff/Eslint/Semgrep/HouseRules/TestCoverage runners
│   ├── openai_protocol.py     # shared OpenAI chat-completions request/parsing/loop, used by Tiers 2 and 3 (and reused by suggest.py)
│   ├── local_llm.py           # Tier 2: local OpenAI-compatible server (LM Studio, Ollama, ...), no cost
│   └── cloud_llm.py           # Tier 3: Anthropic + OpenAI-compatible (Groq/Mistral/Cerebras/OpenRouter/custom) backends, sync httpx, tool-forced JSON, audit cap
├── checks/
│   ├── base.py                 # HouseCheck ABC
│   ├── no_bare_except.py       # RULE-001
│   ├── no_sql_string_format.py # RULE-002
│   ├── whitelist_permission_check.py, n_plus_one_query.py, no_manual_commit.py,
│   │   missing_translation.py, leftover_print.py, silent_exception.py,
│   │   hardcoded_credential.py                        # RULE-003..009 (Python)
│   ├── js_hardcoded_html.py, js_inline_style.py, js_console_debugger.py,
│   │   js_jquery_dom.py, js_frappe_call_error_handling.py,
│   │   js_hardcoded_credential.py                      # RULE-010..016 (JS)
│   ├── method_too_long.py                              # RULE-018 (Python + async def)
│   ├── hooks_structure.py, doctype_schema.py, blocking_call_in_doc_event.py,
│   │   save_in_loop.py, commented_code.py (Python half), magic_number.py,
│   │   missing_docstring.py, boolean_flag_param.py                    # RULE-020..026, 030, 032..034
│   ├── js_async_await_suggestion.py, js_direct_dom.py,
│   │   js_client_script_length.py, commented_code.py (JS half)        # RULE-027..029, 031
│   ├── frappe_db_field_check.py                        # RULE-019 (opt-in, needs --frappe-db-config)
│   ├── secrets_in_repo.py                               # RULE-035 (opt-out via rules.secrets_scan)
│   └── registry.py             # ALL_CHECKS list + load_extra_checks() for rules.extra_checks
│       # RULE-017 (test coverage) lives in reviewers/rules_engine.py, not here --
│       # it's a diff-level SubRunner, not a per-file HouseCheck (see above).
│       # RULE-019 and RULE-035 are also excluded from ALL_CHECKS/registry.py, for
│       # the same reason plus one more each: RULE-019 needs a live DB connection
│       # at construction time, and RULE-035 needs the whole repo tree at once, not
│       # one file -- rules_engine.py wires both in directly (see below) rather
│       # than via the no-argument HouseCheck instantiation the registry assumes.
├── frappe_db.py                # FrappeDbConnection -- read-only connection to a live Frappe site's DB, for RULE-019
└── reporters/
    ├── console.py, json_report.py, markdown_report.py, docx_report.py, xlsx_report.py
    └── glossary.py                 # format_ist() + tier/source description lookups, shared by the above (not json_report.py)
tests/                        # pytest, CLI-level tests via typer.testing.CliRunner
```

## Running tests

```bash
uv run pytest tests/ -q
```

---

[← Documentation index](Home.md)
