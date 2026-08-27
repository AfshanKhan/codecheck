# CLI usage — two modes

There are two commands, `diff` and `audit`, plus two top-level flags that work
before either: `codecheck --help` (lists the commands; `codecheck <command>
--help` lists that command's flags) and `codecheck --version` (prints the
installed version — handy in bug reports).

### `codecheck diff` — review a diff

```bash
uv run codecheck diff --repo-path . --base-ref main
uv run codecheck diff --repo-path . --staged
uv run codecheck diff --repo-path . --pr 123
uv run codecheck diff --pr https://github.com/org/repo/pull/123
uv run codecheck diff --repo-url https://github.com/org/repo --branch develop --base-ref main
uv run codecheck diff --repo-path . --base-ref main --cloud --output-dir ./reports
uv run codecheck diff --repo-path . --base-ref main --local   # local server, no API key
```

Reviews staged changes, a `base-ref...HEAD` diff (e.g. a feature branch against
`main` before merging), or a GitHub PR. Findings are scoped to lines actually
touched by the diff.

| Flag | Default | Meaning |
|---|---|---|
| `--repo-path` | `.` | Path to the git repo. Ignored if `--repo-url` is given, or if `--pr` is a full URL (see below). |
| `--repo-url` | none | Clone this URL to a temp dir first, review it there, delete the clone on exit. Not needed if `--pr` is already a full URL — it implies the repo. |
| `--branch` | remote's default | Which branch to clone when using `--repo-url` (a `--single-branch` clone of just that branch). Ignored without `--repo-url` — for a local `--repo-path`, just check out the branch yourself first. |
| `--base-ref` | `main` | Ref to diff against. Ignored if `--staged` is set. With `--pr`, this becomes an override — normally the PR's actual base branch is resolved automatically (see below). |
| `--staged` | off | Review staged changes (`git diff --cached`-equivalent) instead of a base-ref diff. Mutually exclusive with `--pr`. |
| `--pr` | none | Review a GitHub PR. Pass **the full PR URL** (`https://github.com/org/repo/pull/123`) to review it directly — no `--repo-path`/`--repo-url` needed, it clones the repo the URL points at. Or pass **just the number** (`123`) to review a PR against `--repo-path`'s existing `origin` remote, without cloning anything new. See "Reviewing a GitHub PR" below. |
| `--config` | none | Path to a `config.yaml`. **Not auto-discovered** — see [Configuration](Configuration.md). |
| `--cloud` | off | Turns on Tier 3. ORs with `cloud.enabled` in config — either one is enough to enable it. |
| `--local` | off | Turns on Tier 2 (a local OpenAI-compatible server). ORs with `local.enabled` in config. |
| `--force-cloud` | off | Bypasses the cloud-tier file-count cap (`cloud.audit_file_cap`) for a large diff/PR — see the cost cap note under `audit`. |
| `--force-local` | off | Skip the confirmation prompt/refusal when the local tier would run on this machine instead of a confirmed LM Link remote — see LM Link under [Tier 2 in Architecture](Architecture.md). |
| `--device` | none | Which device to use when `local.model` is loaded on more than one at once (a device name, or `local`). Sets LM Studio's LM Link preferred device — see LM Link under [Tier 2 in Architecture](Architecture.md). |
| `--output-dir` | `./reports` | Where each run's own timestamped subdirectory of JSON/markdown/docx/Excel reports lands — see "Report filenames" below. |
| `--resume-from` | none | Path to a prior run's `report.json` — see "Resuming after a rate limit" below. |
| `--gate` | none | Override `thresholds.fail_on_severity` with a named profile (`strict`/`standard`/`relaxed`) instead of a raw severity value — see "Named gate profiles" below. |
| `--redact` | off | Scrub locally-identifying details (your machine's absolute repo path) from the written report files before saving — see "Sharing a report externally" below. |
| `--suggest-fixes` | off | Ask whichever LLM tier is enabled for a short fix suggestion on findings that don't already have one — see "Fix suggestions" below. |
| `--frappe-db-config` | none | Path to a `site_config.json` for a live Frappe site's database (read-only) — enables RULE-019, which checks DocType field references against that site's real schema. Refused together with `--repo-url`/`--pr` — see "Live Frappe site verification" below. |

#### Reviewing a GitHub PR (`--pr`)

The common case — you have a PR link, not a repo checked out — is a full URL:

```bash
codecheck diff --pr https://github.com/org/repo/pull/123
```

`codecheck` parses the repo and PR number out of the URL itself (also works
against GitHub Enterprise hosts, since only the `/pull/<n>` shape matters, not
the domain), clones that repo to a temp dir, fetches PR #123 from it using
GitHub's `refs/pull/<n>/head` ref — the same ref GitHub exposes for every PR,
open or closed, no API token needed — and deletes the clone on exit. If you
also pass `--repo-url` explicitly, it must point at the same repo as the PR
URL, or `codecheck` refuses with an error rather than silently picking one.

If you already have the repo cloned locally and just want to check out a PR
against its existing `origin` remote (no fresh clone), pass just the number
instead:

```bash
cd my-local-clone
codecheck diff --pr 123
```

Either way, **your current checkout is never touched.** The fetched PR commit
is checked out into an isolated `git worktree` in a temp directory — your
repo's current branch, staged changes, and working directory are left exactly
as they were. The worktree and the temporary refs `codecheck` created are
deleted automatically when the review finishes (or fails). This works for PRs
from forks too, since GitHub always publishes the `refs/pull/<n>/head` ref
regardless of where the branch actually lives.

The PR's base branch is resolved automatically via the `gh` CLI
(`gh pr view <n> --json baseRefName`) if it's installed and authenticated. If
`gh` isn't available, or the lookup fails, it falls back to `--base-ref` if you
passed one, otherwise `main`. Practically: if your PR targets anything other
than `main` and you don't have `gh` set up, pass `--base-ref` explicitly.

#### Private repos (`--repo-url`, `--pr`)

`codecheck` doesn't handle credentials itself — it just shells out to `git`,
so whatever already lets `git clone`/`git fetch` work on your machine (an SSH
key, `gh auth login`, a `.netrc` entry, a credential helper) works identically
here, for free, with zero setup specific to `codecheck`.

If that fails — e.g. a fresh machine or CI runner with nothing configured yet
for this particular repo — and you're at an interactive terminal, `codecheck`
prompts for a username and token and retries, up to 3 attempts, before giving
up with a clear "repository not accessible" error. The credentials are never
written to disk, never embedded in the clone URL (that would leak into the
cloned repo's `.git/config` and into `ps` output for the whole system while
the clone runs), and never reused beyond that one operation — they're handed
to git in-memory for that single clone/fetch via a short-lived `GIT_ASKPASS`
helper. In a non-interactive context (CI, cron, a piped command) it never
prompts — it fails immediately with the same clear error instead of hanging
forever waiting for input nobody can provide.

**This prompt does not grant access to anything — it only lets you supply
credentials you already have.** `codecheck` never checks who you are or
whether you should be able to see the repo; it just hands your token to
`git`, and GitHub/GitLab's own servers decide whether to allow the
clone/fetch, exactly as if you'd typed the same credentials into `git`
directly. If you don't have access to the private repo yourself, entering a
token — any token — will not make it accessible; you'll still get "repository
not accessible" after 3 attempts. This is also why a wrong-but-plausible
token can't be used to probe whether a private repo exists: GitHub/GitLab
return the identical "not found" error for "doesn't exist" and "exists but
you can't see it," on purpose.

GitLab isn't supported yet — `--pr` only recognizes GitHub's PR URL shape and
fetch convention (`refs/pull/<n>/head`); GitLab merge requests use a different
URL shape and ref (`refs/merge-requests/<n>/head`). `--repo-url` alone (no
`--pr`) works against any git host, GitLab included, since that's just a plain
clone.

### `codecheck audit` — review a whole repo

```bash
uv run codecheck audit --repo-path .
uv run codecheck audit --repo-url https://github.com/org/repo
uv run codecheck audit --repo-url https://github.com/org/repo --branch develop
uv run codecheck audit --repo-path . --cloud
uv run codecheck audit --repo-path . --cloud --force-cloud
uv run codecheck audit --repo-path . --local   # local server, no cost cap needed
```

Reviews every file in the repo — not just what's changed — starting from
`git ls-files` (tracked) plus `git ls-files --others --exclude-standard`
(untracked but not gitignored). No diff exists in this mode, so every line in
every file is in scope, not just changed lines.

| Flag | Default | Meaning |
|---|---|---|
| `--repo-path` | `.` | Path to the git repo. Ignored if `--repo-url` is given. |
| `--repo-url` | none | Clone this URL to a temp dir first, audit it there, delete the clone on exit. Lets you audit a GitHub repo without cloning it yourself first. |
| `--branch` | remote's default | Which branch to clone when using `--repo-url` (a `--single-branch` clone of just that branch). Ignored without `--repo-url`. |
| `--config` | none | Same caveat as `diff` — not auto-discovered. |
| `--cloud` | off | Turns on Tier 3. **See the cost warning below before using this on a large repo.** |
| `--local` | off | Turns on Tier 2 (a local OpenAI-compatible server). No cost cap needed — see below. |
| `--force-cloud` | off | Bypasses the cloud-tier file-count safety cap (`cloud.audit_file_cap`). Required if the repo has more eligible files than the cap. |
| `--force-local` | off | Skip the confirmation prompt/refusal when the local tier would run on this machine instead of a confirmed LM Link remote — see LM Link under [Tier 2 in Architecture](Architecture.md). |
| `--device` | none | Which device to use when `local.model` is loaded on more than one at once (a device name, or `local`). Sets LM Studio's LM Link preferred device — see LM Link under [Tier 2 in Architecture](Architecture.md). |
| `--output-dir` | `./reports` | Where each run's own timestamped subdirectory of JSON/markdown/docx/Excel reports lands — see "Report filenames" below. |
| `--resume-from` | none | Path to a prior run's `report.json` — see "Resuming after a rate limit" below. |
| `--gate` | none | Override `thresholds.fail_on_severity` with a named profile (`strict`/`standard`/`relaxed`) instead of a raw severity value — see "Named gate profiles" below. |
| `--redact` | off | Scrub locally-identifying details (your machine's absolute repo path) from the written report files before saving — see "Sharing a report externally" below. |
| `--suggest-fixes` | off | Ask whichever LLM tier is enabled for a short fix suggestion on findings that don't already have one — see "Fix suggestions" below. |
| `--frappe-db-config` | none | Path to a `site_config.json` for a live Frappe site's database (read-only) — enables RULE-019, which checks DocType field references against that site's real schema. Refused together with `--repo-url`/`--pr` — see "Live Frappe site verification" below. |

**Cloud cost cap (both modes):** the cloud tier makes one API call per file. An
`audit` can mean one call per file in the *entire repo*, and a `diff` is only
"naturally small" when you trust its author — an attacker-controlled PR
(`--repo-url`/`--pr`) can touch arbitrarily many files. To guard against an
accidental (or hostile) large bill, both `audit --cloud` and `diff --cloud`
refuse to run if the number of eligible files exceeds `cloud.audit_file_cap`
(default `50`) — they print the file count and exit with code `2` **before
making any API calls**. Pass `--force-cloud` to proceed anyway, or lower
`cloud.audit_file_cap` / point `--repo-path` at a subdirectory to shrink the scope.

**Rate limits are handled automatically — no flag needed.** Free-tier cloud
providers (Groq's free tier is the confirmed case: 12,000 tokens/minute) often
can't cover a whole-repo `audit` at full speed — individual requests get a
`429 Too Many Requests`. When that happens, `codecheck` itself waits (honoring
the provider's `Retry-After` header when given, exponential backoff otherwise,
up to 5 attempts per file) and retries that request in place before moving on
— so a single `codecheck audit --cloud` invocation runs to completion
unattended instead of leaving most files skipped. This is deliberate: the
first version of this only skipped rate-limited files and expected you to
notice and re-run the command, which real testing against Groq showed doesn't
even work as a manual workaround — `codecheck` always processes files in the
same order, so a fresh retry just re-hits the same first few files and stalls
at the same point every single time. Now it just works — kick off `audit
--cloud --force-cloud` and walk away.

**`--resume-from` (both modes) is the fallback for when a run doesn't finish
in one invocation** — interrupted (Ctrl-C, machine went to sleep), or a
provider whose rate limit is so restrictive that even the automatic in-process
retries don't converge within a single run. Point `--resume-from
<path-to-prior-run's-.json-report>` at a previous run's own JSON output (see
"Report filenames" below for what that file is actually called), and any file
the cloud (or local) tier already got a real result for is skipped (not
re-requested) — its prior result is carried into the new report — while only
files that were actually skipped last time get retried. Verified directly
against a real rate-limited Groq run: a repo where only 5/68 files succeeded
went to 9/68 cumulative on a resumed retry, correctly skipping those first 5
instead of repeating them. Only applies to the LLM tiers — the rules tier is
free and fast enough to just re-run in full every time.

### Report filenames

Every run (`diff` or `audit`) gets its own subdirectory inside `--output-dir`
(default `./reports`), named `<repo>_pr<N>_<timestamp>` for a `--pr` run or
`<repo>_<mode>_<timestamp>` otherwise (never both — the PR number replaces
the mode in the name, it isn't appended alongside it), e.g.
`./reports/codecheck_pr12_20260814_161000/` or
`./reports/codecheck_diff_20260814_161000/`. Four files land inside it —
a `.json`, a `.md`, a `.docx`, and a `.xlsx`, same underlying findings, four
formats, each named after the directory they're in
(`codecheck_pr12_20260814_161000.json`, etc.). Runs are never mixed
together loose in one flat directory — `--output-dir` fills up with one
subdirectory per run instead of four files per run all in the same place,
which turns unreadable fast once there's more than a couple of runs sitting
there.

`<repo>` is parsed from `--repo-url`/the PR's URL when one is given (a cloned
repo lands in a randomly-named temp directory, so the directory name itself
isn't useful), otherwise it's `--repo-path`'s own directory name. Each run
gets its own timestamp, so re-running never silently overwrites a previous
run's report — pass the exact `.json` path to `--resume-from` when you want
to continue one. The timestamp only has second resolution, so if a second run
for the same repo/PR/mode finishes within the same second, `-2`, `-3`, ... is
appended to the directory name to keep it unique rather than overwriting the
earlier run's files. That directory is claimed atomically (an exclusive
directory-create, not a check-then-write) — the whole run's worth of files
sits behind one atomic claim now, rather than four separate per-extension
claims — so two `codecheck` processes finishing in the same second against
the same repo/PR/mode can't both win the same directory, and a leftover
partial directory from an earlier interrupted run can't get silently reused.
Any failure during this process — a directory-name collision, a non-collision
I/O error (permission denied, disk full), or a reporter raising once writing
actual content starts — deletes the whole run directory (whatever did or
didn't get written into it) rather than leaving it behind. This rollback is
a normal Python exception handler, though, not a crash-proof guarantee: a
process kill or forced termination (SIGKILL, power loss) partway through can
still leave an empty or partial run directory that no later run can reclaim
(the atomic-create in the next run just sees "already exists" and skips to
`-2`, `-3`, ...) — a handled failure never leaves stale files behind, but an
unhandled one bypassing Python entirely still can.

The `.xlsx` report is the one built for filtering and sorting rather than
reading top to bottom: a "Findings" sheet with every finding as its own row
and a header-row AutoFilter already turned on (severity/check/file dropdowns,
no manual setup needed), plus a "Summary" sheet breaking counts down by
severity and by check ID. Every cell that can hold text derived from the
reviewed repo itself (a file path, a finding's title/explanation/suggestion)
is written Text-formatted with any leading `=`/`+`/`-`/`@` neutralized, so a
report built from an untrusted `--repo-url`/`--pr` can't smuggle a formula
that runs when someone just opens the file in Excel.

Every report format aside from `.json` is meant to be readable by someone who
didn't run the review themselves, so:

- **`Repo`** shows the actual `--repo-url` (or the URL `--pr` was given as)
  when one was passed — not a local temp clone's meaningless directory name
  — and only falls back to a local filesystem path for a genuinely local run
  (`--repo-path`, or `--pr <number>` against an existing local checkout).
  `--redact` leaves a remote URL untouched (nothing local-identifying about
  a public URL) and only replaces an actual local path with a placeholder.
- **`Generated`** is shown in IST (`27 Aug 2026, 12:16 PM IST`), not a raw
  UTC ISO-8601 timestamp. `.json`'s `generated_at` field is the one
  exception, kept as the machine-readable UTC string `render`/`compare`/
  `--resume-from` parse back — see [Architecture](Architecture.md#making-a-report-readable-to-someone-who-didnt-run-it).
- **`Tiers run`** and every check's `(house)`/`(ruff)`/... source tag get a
  one-line "what does this mean" explanation next to them (only for the
  tiers/sources actually present in that run), so a report doesn't assume
  the reader already knows codecheck's own internal vocabulary.

### Named gate profiles (`--gate`)

`thresholds.fail_on_severity` (default `high`) controls the exit-code gate —
what counts as "bad enough to fail the run." `--gate` is a shorthand for
setting it without a raw severity value or a config edit:

```bash
codecheck diff --repo-path . --base-ref main --gate strict    # fails at MEDIUM+
codecheck audit --repo-path . --gate relaxed                  # fails at CRITICAL only
```

| Profile | Equivalent `fail_on_severity` | When to reach for it |
|---|---|---|
| `strict` | `medium` | A security-sensitive repo, or a final pre-release check where you want every non-trivial finding to block. |
| `standard` | `high` | The default — same as not passing `--gate` at all. |
| `relaxed` | `critical` | A legacy codebase still working down a large backlog; only the most severe findings should block merges while everything else gets fixed gradually. |

`--gate` only changes the exit code — every tier still runs the same way and
every finding still shows up in the report and the console output regardless
of severity; this just moves the line for what makes the process exit `1`.

### Sharing a report externally (`--redact`)

The written `.json`/`.md`/`.docx`/`.xlsx` reports include `repo_path` — on a local
run, this is your machine's absolute filesystem path (e.g.
`/Users/yourname/work/some-project`), which can reveal your username and
directory layout to anyone who receives the report file. `--redact` replaces
it with a placeholder before writing (keeping just the repo's own name, e.g.
`<local repo> (some-project)`), and scrubs any other absolute path that shows
up inside a `skipped` entry's message (a linter's own error text can echo one
back). Nothing else in a report is touched — file paths, check IDs, titles,
and explanations are already repo-relative or generic.

```bash
codecheck diff --repo-path . --base-ref main --redact
```

The terminal output for the run you're watching is never redacted, only the
files written to `--output-dir` — redaction only matters once a report leaves
your machine.

### Re-rendering a report (`codecheck render`)

```bash
codecheck render ./reports/myrepo_audit_20260814_161000.json --output-dir ./reports
codecheck render ./reports/myrepo_audit_20260814_161000.json --redact --output-dir ./shared
```

Rebuilds the `.md`/`.docx`/`.xlsx` (and a fresh `.json`) from an existing
report's JSON, without re-running any checks — useful after upgrading `codecheck` (to
pick up a newer reporter's formatting on an old report) or to produce a
`--redact` copy of a report you already generated, without repeating the
whole review. The new files get their own timestamped filename in
`--output-dir`, same naming convention as a live run — the original files are
never modified.

### Comparing two reports (`codecheck compare`)

```bash
codecheck compare ./reports/baseline_audit_20260101_090000.json ./reports/latest_audit_20260814_090000.json
codecheck compare baseline.json latest.json --gate strict
```

Every `diff`/`audit` run is a snapshot — `compare` looks at two of them side
by side and shows what changed: findings present in the second report but not
the first (**newly introduced**), and findings present in the first but not
the second (**resolved**). A finding's identity across the two reports is
`(file, check_id, line number)` — close enough that unrelated code shifting a
few lines around it doesn't spuriously mark it as both resolved and new, but
specific enough that two different issues don't get confused for one.

Typical use: pin an `audit` of your `main` branch as a baseline periodically
(weekly, or after a big cleanup), then `compare` a later audit against it to
see whether the codebase is trending better or worse — a single snapshot
report can't answer that on its own. Exits `1` if any *newly introduced*
finding is at or above `thresholds.fail_on_severity` (or `--gate`'s override)
— wire it into a scheduled CI job to catch a codebase regressing over time,
not just review one point-in-time state.

### Fix suggestions (`--suggest-fixes`)

Most Tier 1 findings (ruff, house rules) come with a title and an explanation
of *what's* wrong, but not a ready-made fix. `--suggest-fixes` is an opt-in
second pass, after the normal review, that asks whichever LLM tier is already
enabled (`--cloud` preferred if both are on, otherwise `--local`) for a short,
targeted fix on each eligible finding — one focused question per finding
("how would you fix this exact, already-identified issue"), not a full
independent re-review of the file. That narrower scope is deliberate: it's
cheaper than Tier 2/3's normal file-wide review, and answering one specific
question is less prone to a plausible-sounding wrong answer than an
open-ended "find problems in this file" prompt.

```bash
codecheck diff --repo-path . --base-ref main --cloud --suggest-fixes
codecheck audit --repo-path . --local --suggest-fixes
```

Requires `--cloud` or `--local` (or the equivalent `cloud.enabled`/
`local.enabled` in config) — with neither enabled there's no LLM to ask, and
the run proceeds normally with a note under "Skipped" rather than failing.
Capped at `suggestions.max_per_run` findings per run (default `5`,
highest-severity first — see [Configuration](Configuration.md)), and
`suggestions.exclude_checks` can list check IDs to never send to the LLM at
all (e.g. one whose finding already contains everything needed to fix it, or
one you'd rather not hand to a cloud provider). A finding that already has a
`suggestion` from its own tier (e.g. a cloud/local-tier finding that came with
one already) is left alone.

### Live Frappe site verification (`--frappe-db-config`)

Every other check in `codecheck` is purely static — it never executes code
and never needs credentials for anything beyond the git auth you already have
configured. `--frappe-db-config` is a narrow, opt-in exception: point it at a
Frappe site's `site_config.json` and `codecheck` opens a **read-only**
connection to that site's live MariaDB database, which lets RULE-019 catch
something no static analysis can ever know — a reference to a DocType field
that doesn't actually exist on that site (renamed, removed, or just never
existed in the first place):

```bash
codecheck audit --repo-path ~/frappe-bench/apps/my_app \
  --frappe-db-config ~/frappe-bench/sites/mysite.local/site_config.json
```

RULE-019 checks the statically-resolvable cases: `frappe.db.get_value`/
`set_value` with a literal doctype and literal fieldname(s), and
`frappe.get_all`/`get_list` with a literal doctype and a literal `fields=[...]`
list. It reads `db_name`/`db_password`/`db_type`/`db_host`/`db_port` straight
out of the `site_config.json` you point it at (the same file your bench
already uses) — nothing is prompted for or stored, and every query it issues
is a hardcoded `SELECT` against Frappe's own schema tables (`tabDocType`,
`tabDocField`, `tabCustom Field`) with parameterized values. It never writes
to the database and never runs arbitrary SQL.

**Refused together with `--repo-url`/`--pr`.** Querying a live database with
values derived from code you don't control and trust (an external PR, a
cloned URL) is a materially different risk than pointing it at your own local
bench's checkout, so `codecheck` refuses the combination outright rather than
letting you opt into it by accident.

Requires the `pymysql` extra: `pip install codecheck[frappe-db]` or `uv sync
--extra frappe-db`. Without it (or with a `site_config.json` this can't parse,
or a database it can't reach), `codecheck` records a clear skip reason under
"Skipped" and the rest of the run proceeds normally — it never aborts the
whole review over this one optional check. Currently supports `db_type:
mariadb` only, the Frappe default.

### Exit codes

`diff` / `audit`:

- `0` — ran clean, or found nothing at/above the fail threshold.
- `1` — at least one finding at or above `thresholds.fail_on_severity` (default
  `high`, or `--gate`'s override). This is what you'd wire into CI.
- `2` — a usage/safety error (bad `base_ref`, no merge base, an invalid
  `--gate` value, or the audit cloud cap blocked the run). No report is
  written in this case. A `base_ref` naming a ref that doesn't exist at all
  used to crash with a raw Python traceback instead — `git.GitCommandError`
  from `repo.merge_base()` wasn't caught alongside the other `ValueError`
  cases in `diff.py`/`cli.py`. Fixed: `get_diff()` now converts it into the
  same clean `ValueError` → `Error: ...` + exit `2` path.

`render`:

- `0` — the report was re-rendered successfully.
- `2` — the given path doesn't exist, isn't readable, or isn't valid JSON.

`compare`:

- `0` — ran clean, or no newly introduced finding meets the fail threshold.
- `1` — at least one newly introduced finding is at or above
  `thresholds.fail_on_severity` (or `--gate`'s override).
- `2` — either given path doesn't exist, isn't readable, or isn't valid JSON.

---

[← Documentation index](Home.md)
