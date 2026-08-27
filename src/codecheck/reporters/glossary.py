"""Shared text for report glossary bits: what "Tiers run" means, and what
the "(house)"/"(ruff)"/... source label refers to. Every reporter renders
this same content so it never drifts out of sync.

Also the one place `generated_at` gets converted to a human timezone for
display -- the JSON report keeps it as raw UTC ISO-8601 for other tools."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# India Standard Time -- fixed +05:30 offset, no DST.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def format_ist(dt: datetime) -> str:
    """E.g. "27 Aug 2026, 12:16 PM IST". A naive datetime is treated as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


TIER_DESCRIPTIONS = {
    "rules": "deterministic linters + codecheck's own built-in rules -- fast, free, no AI involved",
    "local_llm": "an AI model reviewed the code too, running on your own machine or local server",
    "cloud_llm": "an AI model reviewed the code too, via a cloud provider",
}

# Matches the `source` value every Finding actually carries.
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
