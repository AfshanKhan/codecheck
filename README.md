# codecheck

A tool that automatically checks code for problems — bugs, security issues,
bad style — either for a single change (a pull request) or for a whole
codebase. It runs on your own computer or a shared server, not in someone
else's cloud, unless you explicitly turn that on.

It works in three layers ("tiers"), and you choose which ones run:

| Tier | What it is | Cost | Runs by default? |
|---|---|---|---|
| **Tier 1 — Rules** | Fast, mechanical checks (a spell-checker for code) | Free, always | ✅ Yes |
| **Tier 2 — Local AI** | An AI model running on your own machine or a machine you control | Free (your own compute) | ❌ No — opt in with `--local` |
| **Tier 3 — Cloud AI** | An AI model hosted by a provider (Anthropic, Groq, etc.) | May cost money, depending on provider | ❌ No — opt in with `--cloud` |

Tier 1 always runs and never needs an internet connection or an account.
Tiers 2 and 3 are entirely optional, and `codecheck` never contacts a cloud AI
service unless you explicitly ask it to with `--cloud`.

> **⚠️ Security:** `codecheck` can review code you don't control (`--repo-url`,
> `--pr`), and reviewing a repo runs tooling that treats its contents as input —
> some of which (e.g. ESLint's config) executes code from the repo. Only point
> `codecheck` at code you trust, or run it in a sandbox. See
> [SECURITY.md](SECURITY.md) for the threat model and recommended settings for
> untrusted sources.

---

## Quick Start — for everyone

This section assumes no prior experience with command-line tools beyond
opening a terminal and pasting commands into it. If you get stuck, ask
whoever shared this tool with you, or see [Troubleshooting](#troubleshooting)
below.

### 1. Get the code and install it

First, get a copy of this repository. If you have `git`:

```bash
git clone <repo-url>     # replace <repo-url> with the repository's address
cd codecheck
```

(Or, if someone shared the `codecheck` folder with you another way — a zip, a
shared drive — just unpack it and `cd` into that folder instead. And once a
version is released, you can skip cloning entirely and download a prebuilt build
from the project's **Releases** page — see the [Installation guide](docs/Installation.md).)

You also need `uv` (a Python tool installer). If you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then, from inside the `codecheck` folder:

```bash
uv tool install --editable .
```

That's it — you now have a `codecheck` command you can run from **any**
folder on your computer, not just this one. Test it:

```bash
codecheck --help
```

If you see a list of commands, it worked.

### 2. Review a change before merging it (most common use)

Go into the project folder you want to check (the one with the code you
changed), and run:

```bash
codecheck diff --repo-path . --base-ref main
```

This compares your current changes against the `main` branch and reports
anything it finds. Replace `main` with whatever your team's default branch is
called (sometimes `master`).

Already committed your changes and want to check them before pushing? Same
command works. Just staged some changes and haven't committed yet?

```bash
codecheck diff --repo-path . --staged
```

### 3. Review an entire codebase (not just a change)

```bash
codecheck audit --repo-path .
```

This checks every file in the project, not just what changed. Useful for a
first pass on an existing codebase, or a periodic health check.

### 4. Reading the report

After either command, you'll see a table directly in your terminal, like this:

```
╭─ app/api.py ───────────────────────────────────────────╮
│  Line   Severity   Check              Title             │
│  42     HIGH       RULE-002 (house)   frappe.db.sql()    │
│                                        built with string  │
│                                        formatting          │
╰──────────────────────────────────────────────────────────╯

1 finding(s) across 1 file(s) — 1 high
```

- **Severity** tells you how serious it is: `CRITICAL`/`HIGH` (fix before
  merging), `MEDIUM` (worth fixing), `LOW`/`INFO` (minor, style-level).
- **Check** is an ID you can search for if you want more detail (`RULE-002`
  means house rule #2 — see "What does each tier actually check?" below).
- Two report files are also saved for you, every time: `reports/report.md`
  (readable, good for pasting into a PR description or Slack) and
  `reports/report.json` (for other tools to read).

If it says **"No findings"**, nothing was wrong — you're done.

The command also **exits with an error code** if it finds anything serious
(`HIGH` or above by default) — this is what lets it be wired into an
automated PR check later, so a bad change can be blocked automatically. As a
human running it yourself, you can mostly ignore this — just read the report.

### 5. Turning on AI review (optional, off by default)

Tier 1 (the free, automatic checks) catches a lot, but it can't reason about
your code the way a person — or an AI — can. If you want deeper review:

**Option A — a free AI provider (no signup cost, no credit card):**

```bash
export GROQ_API_KEY="your-key-here"   # get one free at console.groq.com
codecheck diff --repo-path . --base-ref main --cloud
```

(You need a `config.yaml` set up first for this — see [Configuration](docs/Configuration.md). Someone on your team may have
already prepared one for you to copy.)

**Option B — an AI model on your own machine (also free, needs decent
hardware):** if your team runs a shared tool like LM Studio or Ollama, ask
whoever set it up for the two settings you need (which "provider" and
"model"), put them in your `config.yaml`, then:

```bash
codecheck diff --repo-path . --base-ref main --local
```

Either way, `codecheck` will tell you clearly in its output if the AI step
couldn't run for some reason (missing key, server not reachable, etc.) — the
free checks from Tier 1 still ran regardless, so you're never left with
nothing.

### What does each tier actually check?

**Tier 1 — Rules (always on, free)**

| Check | What it looks for |
|---|---|
| `ruff` | Python style issues, common bugs, and a set of security-relevant patterns (unsafe `eval`, hardcoded passwords, etc.) |
| `eslint` | JavaScript/TypeScript issues — only runs if your project already has an eslint setup |
| `semgrep` | Security patterns across many languages (SQL injection shapes, unsafe deserialization, etc.) |
| `RULE-001` (house rule) | Bare `except:` blocks, which silently swallow errors including ones you'd want to know about |
| `RULE-002` (house rule) | Building a database query by directly inserting a variable into the text (SQL injection risk), instead of using safe parameters |

**Tier 2 / Tier 3 — AI review (opt-in)**

Both AI tiers use the same instructions under the hood: look for logic bugs,
edge cases the code doesn't handle, security issues, and (if relevant)
Frappe/ERPNext-specific mistakes — and explicitly ignore style/formatting,
since Tier 1 already covers that. The difference between Tier 2 and Tier 3 is
only *where* the AI model runs (your machine vs. a cloud provider), not what
it looks for.

### Troubleshooting

- **"command not found: codecheck"** — the install step didn't finish, or you
  need to open a new terminal window for the `codecheck` command to be found.
  Re-run `uv tool install --editable .` from inside the `codecheck` folder.
- **"No changed files found"** — you ran `codecheck diff` but there's nothing
  different from the base branch. Check you're on the right branch, or that
  you've actually made changes.
- **A finding you don't understand** — search this README for its check ID
  (e.g. `RULE-002`), or ask in your team's channel for this tool.
- **The AI tier didn't run** — look for a line under "Skipped:" in the
  output; it always explains why (missing key, server unreachable, etc.).
  The free Tier 1 checks still ran either way.
- **Something else** — see [Known limitations](docs/Known-Limitations.md), or
  ask whoever maintains this tool for your team.

**One safety note if you use `--repo-url`/`--pr` on a repo you don't fully
trust** (e.g. a stranger's fork): the ESLint check runs code from that repo
(its lint config) as part of reviewing it. See [SECURITY.md](SECURITY.md) (or the deeper
[Security notes](docs/Security.md)) before pointing this at code you don't trust — for your
own team's PRs this isn't a concern.

---

## Documentation

Full reference and deep-dive docs live in [`docs/`](docs/Home.md) (and are
mirrored to the project wiki):

- **[Installation](docs/Installation.md)** — every install option + how to publish a release
- **[CLI reference](docs/CLI-Reference.md)** — every `diff` / `audit` flag, exit codes, `--help` / `--version`
- **[Configuration](docs/Configuration.md)** — the full `config.yaml` schema + cloud/local provider setup
- **[Architecture & internals](docs/Architecture.md)** — how the tiers, rules engine, LLM reviewers, aggregator, and reporters work
- **[Security](docs/Security.md)** and top-level **[SECURITY.md](SECURITY.md)** — trust model and what's hardened
- **[Verification status](docs/Verification-Status.md)** — what's tested against live instances vs. expected to work
- **[Known limitations](docs/Known-Limitations.md)** — the honest v1 caveats

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and
how to run the tests, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Report security
issues privately per [SECURITY.md](SECURITY.md), not via a public issue.

## License

[Apache License 2.0](LICENSE).
