# Agent Guidelines

Welcome to `codecheck`. This repository provides automated code review across 3 tiers: mechanical rules, local AI models, and optional cloud LLMs.

## Navigation & tooling

- **Domain language**: [`GLOSSARY.md`](GLOSSARY.md) — canonical terms (`ReviewSource`, `ReviewEngine`, `ReportBundle`, `AstRule`).
- **Architecture & seams**: [`docs/Architecture.md`](docs/Architecture.md) — module layout, check registry, and tier responsibilities.
- **Checks**: run via `uv run` (`uv run ruff check`, `uv run ty check` on core modules, `uv run pytest`).

## Agent skills

### Issue tracker

GitHub issues via `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical 5-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout (`GLOSSARY.md` + `docs/adr/` at repo root). See `docs/agents/domain.md`.
