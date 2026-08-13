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
| `--output-dir` | `./reports` | Where `report.json` and `report.md` are written. |
| `--resume-from` | none | Path to a prior run's `report.json` — see "Resuming after a rate limit" below. |

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
| `--output-dir` | `./reports` | Where `report.json` and `report.md` are written. |
| `--resume-from` | none | Path to a prior run's `report.json` — see "Resuming after a rate limit" below. |

**Cloud cost cap (both modes):** the cloud tier makes one API call per file. An
`audit` can mean one call per file in the *entire repo*, and a `diff` is only
"naturally small" when you trust its author — an attacker-controlled PR
(`--repo-url`/`--pr`) can touch arbitrarily many files. To guard against an
accidental (or hostile) large bill, both `audit --cloud` and `diff --cloud`
refuse to run if the number of eligible files exceeds `cloud.audit_file_cap`
(default `50`) — they print the file count and exit with code `2` **before
making any API calls**. Pass `--force-cloud` to proceed anyway, or lower
`cloud.audit_file_cap` / point `--repo-path` at a subdirectory to shrink the scope.

**Resuming after a rate limit (`--resume-from`, both modes):** free-tier cloud
providers (Groq's free tier is the confirmed case: 12,000 tokens/minute) often
can't cover a whole-repo `audit` in one run — most of the files just get
skipped with a `429 Too Many Requests`. Since `codecheck` always processes
files in the same order, simply re-running the same command doesn't help: it
burns the fresh rate-limit budget on the exact same first few files every
time and never reaches the rest. `--resume-from <path-to-prior-report.json>`
fixes this — any file the cloud (or local) tier already got a real result for
in that prior report is skipped (not re-requested) and its prior result is
carried into the new report; only files that were actually skipped last time
get retried. Repeating `--cloud --force-cloud --resume-from reports/report.json`
a few times in a row (pointing each one at the previous run's own output)
converges on full coverage instead of stalling. Verified directly against a
real rate-limited Groq run: a repo where only 5/68 files succeeded on the
first attempt went to 9/68 cumulative on a resumed retry, correctly skipping
those first 5 and picking up new ones instead of repeating them. Only applies
to the LLM tiers — the rules tier is free and fast enough to just re-run in
full every time.

### Exit codes (both modes)

- `0` — ran clean, or found nothing at/above the fail threshold.
- `1` — at least one finding at or above `thresholds.fail_on_severity` (default
  `high`). This is what you'd wire into CI.
- `2` — a usage/safety error (bad `base_ref`, no merge base, or the audit cloud
  cap blocked the run). No report is written in this case. A `base_ref` naming
  a ref that doesn't exist at all used to crash with a raw Python traceback
  instead — `git.GitCommandError` from `repo.merge_base()` wasn't caught
  alongside the other `ValueError` cases in `diff.py`/`cli.py`. Fixed: `get_diff()`
  now converts it into the same clean `ValueError` → `Error: ...` + exit `2` path.

---

[← Documentation index](Home.md)
