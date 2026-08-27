"""Shared, single-source-of-truth text for the bits of a report a first-time
reader has no way to already know: what "Tiers run" means, and what the
"(house)"/"(ruff)"/... label after a check ID actually refers to. Every
reporter (console/markdown/docx/xlsx) renders this same content into its own
native format rather than each writing its own wording, so the explanation
never drifts out of sync between formats.

Also the one place `generated_at` (always stored as a UTC-aware datetime --
`datetime.now(timezone.utc)` at review time) gets converted to a human
timezone for *display*. This is a display-only concern: the JSON report
keeps `generated_at` as the raw UTC ISO-8601 string, since that's the
machine-readable format `ReviewReport.from_dict()` parses back via
`datetime.fromisoformat()` for `render`/`compare`/`--resume-from` -- swapping
it for a localized string there would break every consumer of that field,
for a report format that's documented as "for other tools to read," not for
a person to read directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# India Standard Time -- fixed +05:30 offset, no DST, so a plain fixed-offset
# timezone (not a zoneinfo lookup, which would need the system tzdata to be
# present) is both correct and dependency-free.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def format_ist(dt: datetime) -> str:
    """E.g. "27 Aug 2026, 12:16 PM IST" -- readable at a glance, unlike a raw
    ISO-8601 UTC-with-microseconds string. `dt` is assumed UTC-aware (every
    `generated_at` codecheck itself produces is); a naive datetime (e.g. from
    a hand-edited report) is treated as already UTC rather than raising.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


TIER_DESCRIPTIONS = {
    "rules": "deterministic linters + codecheck's own built-in rules -- fast, free, no AI involved",
    "local_llm": "an AI model reviewed the code too, running on your own machine or local server",
    "cloud_llm": "an AI model reviewed the code too, via a cloud provider",
}

# Matches the `source` value every Finding actually carries -- see
# reviewers/rules_engine.py (house/ruff/eslint/semgrep) and
# reviewers/openai_protocol.py (source=self.name, resolving to
# cloud_llm/local_llm for those two tiers).
SOURCE_DESCRIPTIONS = {
    "house": "one of codecheck's own built-in rules (RULE-0xx)",
    "ruff": "the ruff Python linter",
    "eslint": "the eslint JS/TS linter",
    "semgrep": "the semgrep pattern-matching security scanner",
    "cloud_llm": "a cloud AI model's own finding",
    "local_llm": "a local AI model's own finding",
}


def tier_description(tier: str) -> str:
    return TIER_DESCRIPTIONS.get(tier, tier)


def source_description(source: str) -> str:
    return SOURCE_DESCRIPTIONS.get(source, source)
