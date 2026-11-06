from pathlib import Path

import pytest
from pydantic import ValidationError

from codecheck.config import load_config


def test_missing_explicit_config_file_is_an_error_not_silent_defaults(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "typo.yaml")


def test_no_config_path_gives_defaults():
    assert load_config(None).thresholds.fail_on_severity == "high"


def test_fail_on_severity_is_normalized_to_lowercase(tmp_path: Path):
    path = tmp_path / "c.yaml"
    path.write_text("thresholds:\n  fail_on_severity: High\n")
    assert load_config(path).thresholds.fail_on_severity == "high"


def test_invalid_fail_on_severity_is_rejected_at_load_time(tmp_path: Path):
    path = tmp_path / "c.yaml"
    path.write_text("thresholds:\n  fail_on_severity: urgent\n")
    with pytest.raises(ValidationError, match="fail_on_severity"):
        load_config(path)
