# codecheck

Automated code review system providing multi-tiered analysis across mechanical rules, local models, and cloud LLMs.

## Language

**ReviewSource**:
An abstraction representing the code target under review—such as a git diff, a pull request, or a full repository snapshot—isolated for analysis.
_Avoid_: Input, DiffTarget, WorkspacePayload

**ReviewEngine**:
The central coordinator that executes mechanical checks and AI reviewers against a ReviewSource to produce a unified review report.
_Avoid_: ReviewRunner, CheckerManager, Orchestrator

**ReportBundle**:
An atomic collection of review reports generated and rendered across requested output formats.
_Avoid_: ExportPackage, OutputFiles, FormatterBatch

**AstRule**:
A declarative syntax tree pattern matcher evaluated concurrently alongside other rules during a single visitor pass over a file's AST.
_Avoid_: Check, LintPass, LinterPlugin
