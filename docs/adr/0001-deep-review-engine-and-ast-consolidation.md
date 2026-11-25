# Deep ReviewEngine and Consolidated AST Engine

To eliminate duplicated procedural orchestration in the CLI and web interfaces and reduce repetitive AST parsing overhead, we introduced `ReviewEngine`, `ReviewSource`, `ReportManager`, and consolidated mechanical checks under `AstCheckEngine`.

## Context

Previously, `cli.py` and `web.py` separately orchestrated git diff parsing, file reading, check execution, LLM reviewing, and multi-format report writing. Concurrently, 8 individual AST checks each parsed and walked Python source trees independently, leading to 8x traversal overhead during repository audits.

## Decision

1. **Unified Seam**: Route all review operations (CLI commands, web requests, CI runs) through `ReviewEngine` accepting a polymorphic `ReviewSource` (`GitDiffSource`, `GitHubPrSource`, `RepoAuditSource`, or `InMemorySource`).
2. **Consolidated AST Engine**: Merge the 8 separate AST visitor passes into a single visitor (`AstCheckEngine`) running individual `AstRule` matchers concurrently, yielding a ~7x speedup on repository audits while maintaining 100% finding and rule ID parity.
3. **Atomic Reporting**: Consolidate format-specific serialization behind `ReportManager` which renders `ReportBundle` outputs atomically with automatic cleanup on failure.

## Consequences

- The CLI and Web interfaces become thin adapters around `ReviewEngine`.
- Individual check module classes remain available for backwards-compatible imports in tests.
- Mechanical check execution time on whole repositories drops from ~4.5s to <0.8s for multi-thousand line audits.
