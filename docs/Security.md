# Security considerations

`codecheck` is explicitly designed to fetch and review code you don't
personally control — `--repo-url` clones an arbitrary URL, and `--pr` fetches
an untrusted PR head, both into an isolated location, specifically so you can
review a PR "without ever cloning it yourself." That's a documented, intended
use case, not a hypothetical — so it's worth being precise about what running
`codecheck` against someone else's repo actually does on your machine.

**The main thing to know: the ESLint check executes code from the target
repo.** This isn't a bug to be fixed later, it's inherent to how ESLint
works — running it at all means loading and executing the repo's own
`eslint.config.js`/`.eslintrc.js` as JavaScript, since that's ESLint's config
format. If you're reviewing your own team's PRs, this is no different from
running `eslint` yourself, which you'd do anyway. If you're auditing a repo
you don't trust (a stranger's fork, an unfamiliar dependency), be aware you're
running its code, the same as you would be by running its test suite or build
script locally. `codecheck` does **not** run ESLint from a binary the target
repo ships (`node_modules/.bin/eslint` is deliberately never used — only a
PATH-resolved `eslint` you installed yourself), but the config-execution
behavior above is inherent to ESLint itself and isn't something a wrapper tool
like this can safely paper over. If you want to audit a fully untrusted repo,
set `rules.eslint: false` for that run, or do it inside a disposable
container/VM.

**semgrep's `--config=auto` reaches semgrep's own rule registry over the
network** to fetch the ruleset it runs (unavoidable if you want that curated
ruleset) — `codecheck` passes `--metrics=off` to disable the separate
scan-telemetry semgrep sends by default, but the rule-download request itself
still happens, and it happens for every rules-tier run, not just untrusted
ones. If that's not acceptable for your environment, disable it with
`rules.semgrep: false`, or point semgrep at a local/offline ruleset yourself
outside `codecheck`.

**Things already hardened, so you don't need to think about them further:**
- `git clone`/`git fetch` calls (`--repo-url`, `--pr`) reject URLs starting
  with `-` (argument injection into the git command) and URLs using git's
  transport-helper syntax (a `::` with no `/` before it, e.g. `ext::sh -c '...'`,
  which is how a crafted "URL" can run an arbitrary shell command — ordinary
  URLs and IPv6 literals like `https://[::1]/repo` are unaffected) — both were
  confirmed reachable with no prior validation and are now rejected before any
  subprocess call. All git and linter subprocess calls also pass `--` before
  file/URL arguments, so a file whose name happens to look like a flag can't
  be misinterpreted as one.
- File content sent to a cloud/local LLM is read via a path that's confirmed
  to stay inside the repo being reviewed (`read_file_content` resolves and
  checks containment) — a crafted `../../` path can't make it read or leak a
  file from outside the repo.
- Untrusted text that flows into the reports — attacker-controlled file names,
  linter messages, LLM-generated titles — is escaped before rendering (Markdown
  special characters in `report.md`, rich console markup in the terminal), so a
  crafted repo can't inject clickable links, fake severity styling, or broken
  tables into a report you might paste into a PR or Slack.
- No `eval`/`exec`/`pickle`, no `shell=True`, all subprocess calls use list
  form (not a shell string), TLS verification is never disabled, and API keys
  are read from environment variables only — never written into `config.yaml`
  or logs.

**Private repos and credentials (`--repo-url`, `--pr`):** `codecheck` doesn't
handle credentials itself — it shells out to `git`, so whatever git credential
setup already works on your machine (SSH key, `gh auth login`, `.netrc`, a
credential helper) is tried first, for free. Only if that fails with an
auth-shaped error, and only at an interactive terminal, does `codecheck`
prompt for a username/token — up to 3 attempts before giving a clear
"repository not accessible" error, and it never prompts in a non-interactive
context (CI, cron) since there'd be nobody to answer. The credentials you
enter are never written to disk, never embedded in the clone URL (that would
leak into the cloned repo's `.git/config` and into `ps` output for the whole
system while the clone runs), and never reused beyond that one operation —
they're handed to `git` in-memory via a short-lived `GIT_ASKPASS` script. This
prompt also can't be used to gain access you don't already have: `codecheck`
never checks who you are, it just relays your credentials to GitHub/GitLab's
own servers, which decide whether to allow the clone/fetch — same as if you'd
typed the same credentials into `git` directly. Full details in the "Private
repos" section of [CLI Reference](CLI-Reference.md).

**What isn't sandboxed, by design in v1:** none of the above changes the fact
that `codecheck` runs real, unsandboxed tools (ruff, ESLint, semgrep) against
files from the target repo, on whatever machine you run it on. There's no
container/VM isolation in v1. For your own team's code this is normal — it's
the same trust boundary as cloning the repo and running its linters yourself.
For a genuinely untrusted repo, treat `codecheck audit --repo-url <url>` the
same way you'd treat cloning and building that repo: do it somewhere
disposable, not on a machine with access to anything sensitive.

> This page is the in-depth companion to the repository's top-level [SECURITY.md](../SECURITY.md), which also covers how to report a vulnerability.

---

[← Documentation index](Home.md)
