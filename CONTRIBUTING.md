# Contributing to codecheck

Thanks for helping improve codecheck! Issues and PRs are welcome.

## Development setup

You need [`uv`](https://docs.astral.sh/uv/). Then:

```bash
git clone <repo-url>
cd codecheck
uv sync            # installs runtime + dev deps (pytest, ruff) into .venv
```

Run the tool from source with `uv run codecheck ...`. See the
[Installation docs](docs/Installation.md) for other setups.

## Tests and linting

```bash
uv run pytest          # full test suite
uv run ruff check      # lint (rule set is pinned in pyproject.toml)
uv run ruff check --fix   # auto-fix what's fixable
```

CI runs both on every push and PR ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)),
across Python 3.11–3.13. Please make sure they pass locally first.

> Note: some rules-engine tests shell out to `ruff` by name. `uv run` puts the
> project venv on `PATH`, so run them via `uv run pytest` — a bare `pytest` from
> a shell without the venv activated may not find `ruff` and will skip/fail those.

## What makes a good PR

- Keep changes focused, and add or update tests for behavior changes.
- Match the surrounding style — the code is deliberately commented with the
  *why*, not just the *what*.
- Update the relevant page under [`docs/`](docs/Home.md) if you change behavior,
  flags, or config. (Docs are mirrored to the wiki automatically.)

### Reporting real provider/model results

The verification tables in [`docs/Verification-Status.md`](docs/Verification-Status.md)
only mark a provider ✅ when it's been run against a real, live instance. If you
try a Tier 2/3 provider or model, please update that table — or open a
"Provider / model verification report" issue — with what you actually observed,
including the **server** (LM Studio vs. Ollama vs. llama-server vs. …) and the
**model**, since reliability depends on the combination, not the model name alone.

### Adding a house rule

See [Architecture → House rules](docs/Architecture.md): subclass `HouseCheck`,
implement `check_file(file_path, content, changed_lines)`, and register an instance
in `checks/registry.py`. No other wiring is needed.

## Security

Please report security vulnerabilities **privately**, not via a public issue or
PR — see [SECURITY.md](SECURITY.md). codecheck runs external tooling against the
code it reviews, so changes touching subprocess calls, cloning, or file reads
deserve extra scrutiny; call them out in your PR.

## License

By contributing, you agree that your contributions are licensed under the
project's [Apache License 2.0](LICENSE).
