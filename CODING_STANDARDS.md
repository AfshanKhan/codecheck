# Coding Standards

Judgement-call conventions enforced during `/code-review`. Mechanical formatting, unused imports, and type errors belong to `ruff` and static type checks.

## Architecture & seams

- **Route entrypoints through `ReviewEngine`**: New review triggers (CLI subcommands, web runners, CI hooks) should acquire targets via a `ReviewSource`, execute via `ReviewEngine`, and emit outputs via `ReportManager` instead of duplicating tier loops or file-writing logic.
- **Consolidate Python AST rules**: New single-file Python AST checks belong as an `AstRule` in `src/codecheck/checks/engine.py` (`AstCheckEngine`) rather than standalone `ast.walk()` passes. Retain legacy check classes only where existing test imports depend on them.

## Source & scoping invariants

- **Default diff base**: `GitDiffSource` defaults `base_ref` to `"main"` (matching `codecheck diff`), never `"HEAD~1"`.
- **Source-aware line scoping**: Enforce diff-hunk line filtering (`within_diff_scope`) only when `changed_lines is not None` (diff/PR targets). Repo audits (`changed_lines is None`) must report all findings across audited files.

## Repo hygiene

- **No local paths or personal scripts**: Never commit absolute user paths in `AGENTS.md`, `CLAUDE.md`, or `GEMINI.md` (use `@AGENTS.md`), and keep `.scratch/` or host-specific deploy files out of PR diffs.
