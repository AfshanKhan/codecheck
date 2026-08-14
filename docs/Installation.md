# Setup — all install options

The Quick Start in the [README](../README.md) covers the one recommended path.
Here are all of them, for reference.

All options below assume you've already cloned (or downloaded) this repository
and are running the commands from inside it:

```bash
git clone <repo-url>     # replace <repo-url> with the repository's address
cd codecheck
```

Two ways to use this, depending on whether you're developing `codecheck` itself
or just want the `codecheck` command available everywhere.

### Option A — development mode (working on `codecheck` itself)

```bash
uv sync
cp config.yaml.example config.yaml
```

Run commands with `uv run codecheck ...` from inside this repo (used throughout
the rest of this README). Changes to the source take effect immediately, no
reinstall needed.

### Option B — install as a standalone tool (use it from anywhere)

Verified end to end — this actually produces a working, persistent `codecheck`
binary, not just a claim it should. Use `--editable` so it upgrades itself —
source changes in this repo take effect immediately, no reinstall step, no
version bumping, no `--force` ceremony:

```bash
uv tool install --editable .    # from inside this repo
codecheck --help                 # now on PATH, works from any directory
```

Verified this directly: edited a string in `cli.py`, ran `codecheck --help`
again with zero reinstall in between, and the change was already there.

- **Run without installing at all**: `uvx --from /path/to/codecheck codecheck --help`
- **"Upgrade" after pulling new source changes**: nothing to do — `-e` already
  covers it, since it's the same install pointing at the same directory.
- **If `pyproject.toml`'s dependency list changes** (a new package added, a
  version bumped) — that's the one case editable mode doesn't auto-pick-up:
  `uv tool install --editable . --reinstall`
- **Uninstall**: `uv tool uninstall codecheck`
- **Without `--editable`** (a real snapshot copy, e.g. for something you don't
  want silently tracking repo edits): plain `uv tool install .`, then
  `uv tool install --force .` to update it after changes.

Either way, `ruff`/`eslint`/`semgrep` are **not** bundled by default — a plain
`uv tool install` only pulls in `codecheck`'s own runtime dependencies (typer,
rich, httpx, gitpython, pydantic, pyyaml). This is deliberate: `codecheck`'s
rules-engine sub-runners are "bring your own tool" — it runs whatever
`ruff`/`semgrep` version you already have configured for your project, rather
than forcing a specific pinned version as a hard dependency that could
conflict with your project's own. If you don't already have them, install the
optional `rules` extra to get a reasonable default of both in one command:

```bash
uv tool install --editable ".[rules]"    # or: pip install "codecheck[rules]"
```

`eslint` can't be included this way at all — it's an npm package, not a
Python one, so there's no way to declare it as a Python dependency. Install it
separately (`npm install -g eslint`, or per-project) if you want that
sub-runner. Any of these that are still missing are skipped gracefully (not an
error) — see the rules engine in [Architecture](Architecture.md).

### Option C — build a real wheel, install with plain `pip` (no `uv` at all)

`codecheck` is a standard PEP 517/518 package (hatchling backend, no `uv`-only
magic) — this was verified with an actual clean venv and stock `pip`, not just
`uv`'s own installer:

```bash
uv build                     # produces dist/codecheck-<version>-py3-none-any.whl (+ sdist)
python3.11 -m venv .venv-test
source .venv-test/bin/activate
pip install dist/codecheck-<version>-py3-none-any.whl
codecheck --help
```

(`uv build` is just the build-frontend used to produce the wheel — the
resulting artifact has no `uv` dependency; `python -m build` from the standard
`build` package works identically, since both just call hatchling.)

**Upgrading works exactly like any other `pip` package** — verified directly:
bumped `version` in `pyproject.toml` from `0.1.0` to `0.1.1`, rebuilt, and
`pip install --upgrade dist/codecheck-0.1.1-py3-none-any.whl` correctly
uninstalled `0.1.0` and installed `0.1.1` in its place. There's no package
index (PyPI or private) set up for this project, so "upgrade" today means
"rebuild and point pip at the new wheel file" rather than
`pip install --upgrade codecheck` pulling from a registry automatically — that
would need actually publishing the package somewhere, which hasn't been done.

**`uv pip` is a genuine drop-in for the `pip` commands above, just faster** —
also verified directly, same version-bump-and-upgrade test:

```bash
uv venv --python 3.11 .venv-test
uv pip install --python .venv-test/bin/python dist/codecheck-<version>-py3-none-any.whl
uv pip install --python .venv-test/bin/python --upgrade dist/codecheck-<new-version>-py3-none-any.whl
```

Same `dist/*.whl` from `uv build`, same install/upgrade semantics — `uv pip`
just reimplements the pip CLI surface on `uv`'s faster resolver/installer, it
isn't a different packaging story. Confirmed `uv pip install --upgrade`
correctly bumped `0.1.0` → `0.1.1` in a real venv, same as plain `pip` did
above, noticeably faster.

### Option D — install a published release (no clone, no build)

Once a version is tagged and published under the repo's **Releases** page, you
can install it directly — no need to clone the repo or run `uv build` yourself.

**From the prebuilt wheel attached to the release** — download
`codecheck-<version>-py3-none-any.whl` from the release's Assets, then install
that file:

```bash
uv tool install ./codecheck-<version>-py3-none-any.whl
# pip equivalent: pip install ./codecheck-<version>-py3-none-any.whl
```

**Straight from the tag, without downloading anything** (builds from the tagged
source; needs `git`):

```bash
uv tool install "git+https://github.com/<you>/codecheck.git@v<version>"
# pip equivalent: pip install "git+https://github.com/<you>/codecheck.git@v<version>"
```

A release build is a fixed snapshot (unlike the `--editable` install in Option
B) — to upgrade, install the wheel/tag from a newer release. As with every
option, `ruff`/`eslint`/`semgrep` aren't bundled — install whichever you want on
`PATH` separately (see [Architecture](Architecture.md)).

#### Publishing a release (maintainers)

Build the artifacts and attach them to a GitHub release tagged `v<version>`
(matching `version` in `pyproject.toml`):

```bash
uv build          # writes dist/codecheck-<version>-py3-none-any.whl and the .tar.gz sdist
```

Upload both `dist/*.whl` and `dist/*.tar.gz` as release assets, so users can
install from the wheel (above) or build from the sdist. There's no PyPI index
for this project, so `pip install codecheck` from a registry still won't work —
the wheel/`git+https` installs from a release are the distribution path.

`config.yaml` is gitignored (see `.gitignore`) since it's meant to hold local/repo
overrides; `config.yaml.example` is the checked-in template. Copy it regardless
of which setup option you use — `--config path/to/config.yaml` works the same
either way.

---

[← Documentation index](Home.md)
