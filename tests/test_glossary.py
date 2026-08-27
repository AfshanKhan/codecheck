from datetime import datetime, timezone

from codecheck.reporters.glossary import (
    format_ist,
    source_description,
    tier_description,
)


def test_format_ist_converts_utc_to_ist_offset():
    # IST is UTC+5:30 -- 00:00 UTC is 05:30 AM IST the same calendar day.
    dt = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
    assert format_ist(dt) == "27 Aug 2026, 05:30 AM IST"


def test_format_ist_crosses_midnight_into_next_day():
    dt = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)  # 20:00 UTC -> 01:30 AM IST next day
    assert format_ist(dt) == "28 Aug 2026, 01:30 AM IST"


def test_format_ist_treats_naive_datetime_as_utc():
    dt = datetime(2026, 8, 27, 0, 0)  # no tzinfo
    assert format_ist(dt) == "27 Aug 2026, 05:30 AM IST"


def test_tier_description_known_and_unknown():
    assert "AI" in tier_description("rules") or "no AI" in tier_description("rules")
    assert tier_description("local_llm") != "local_llm"
    assert tier_description("cloud_llm") != "cloud_llm"
    # An unrecognized tier name falls back to itself rather than raising.
    assert tier_description("mystery_tier") == "mystery_tier"


def test_source_description_known_and_unknown():
    for source in ("house", "ruff", "eslint", "semgrep", "cloud_llm", "local_llm"):
        assert source_description(source) != source
    assert source_description("mystery_source") == "mystery_source"
