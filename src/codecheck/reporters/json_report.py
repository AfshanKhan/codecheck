"""JSON reporter, for CI/tooling consumption."""

from __future__ import annotations

import json
from pathlib import Path

from codecheck.models import ReviewReport


def write_json_report(report: ReviewReport, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report.to_dict(), indent=2))
