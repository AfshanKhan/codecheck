from codecheck.aggregator import aggregate
from codecheck.models import Finding, Severity


def make_finding(**overrides) -> Finding:
    defaults = dict(
        check_id="RULE-001",
        tier="rules",
        source="house",
        severity=Severity.MEDIUM,
        title="Bare except clause",
        explanation="explanation",
        file="a.py",
        line_start=10,
        line_end=10,
    )
    defaults.update(overrides)
    return Finding(**defaults)


def test_no_duplicates_across_different_files():
    a = make_finding(file="a.py")
    b = make_finding(file="b.py")
    merged = aggregate({"rules": [a, b]})
    assert len(merged) == 2


def test_dedupes_overlapping_similar_findings_same_file():
    # Two tools describing the same issue in close-enough wording — the case the
    # cheap SequenceMatcher heuristic is meant to catch.
    semgrep_finding = make_finding(
        check_id="SEMGREP-eval-detected", source="semgrep",
        title="Detected use of eval() with user input",
        line_start=10, line_end=10,
    )
    cloud_finding = make_finding(
        tier="cloud_llm", check_id="CLOUD-001", source="cloud_llm",
        title="Use of eval() with untrusted user input",
        line_start=10, line_end=10,
    )
    merged = aggregate({"rules": [semgrep_finding], "cloud_llm": [cloud_finding]})
    assert len(merged) == 1
    assert "also_flagged_by" in merged[0].raw


def test_does_not_dedupe_dissimilar_wording_for_same_underlying_issue():
    # Known v1 limitation: title-only similarity won't catch cases where two
    # tools describe the same issue very differently (e.g. a terse linter code
    # vs. a descriptive house-rule title) — that's a real gap, not a bug.
    ruff_finding = make_finding(
        check_id="RUFF-E722", source="ruff", title="E722: Do not use bare `except`",
        line_start=10, line_end=10,
    )
    house_finding = make_finding(
        check_id="RULE-001", source="house", title="Bare except clause",
        line_start=10, line_end=10,
    )
    merged = aggregate({"rules": [ruff_finding, house_finding]})
    assert len(merged) == 2


def test_does_not_dedupe_dissimilar_titles_even_if_overlapping():
    f1 = make_finding(title="Bare except clause", line_start=10, line_end=10)
    f2 = make_finding(title="Unused variable in loop body", line_start=10, line_end=11)
    merged = aggregate({"rules": [f1, f2]})
    assert len(merged) == 2


def test_cloud_tier_wins_as_primary_and_severity_is_max():
    rules_finding = make_finding(
        tier="rules", source="ruff", check_id="RUFF-S608",
        severity=Severity.MEDIUM, title="Possible SQL injection", line_start=5, line_end=5,
    )
    cloud_finding = make_finding(
        tier="cloud_llm", source="cloud_llm", check_id="CLOUD-001",
        severity=Severity.CRITICAL, title="Possible SQL injection vulnerability",
        line_start=5, line_end=5,
    )
    merged = aggregate({"rules": [rules_finding], "cloud_llm": [cloud_finding]})
    assert len(merged) == 1
    assert merged[0].tier == "cloud_llm"
    assert merged[0].severity == Severity.CRITICAL


def test_sorted_by_severity_desc_then_file_then_line():
    low = make_finding(file="z.py", severity=Severity.LOW, line_start=1, title="A")
    high = make_finding(file="a.py", severity=Severity.HIGH, line_start=5, title="B")
    critical = make_finding(file="a.py", severity=Severity.CRITICAL, line_start=1, title="C")
    merged = aggregate({"rules": [low, high, critical]})
    assert [f.severity for f in merged] == [Severity.CRITICAL, Severity.HIGH, Severity.LOW]
