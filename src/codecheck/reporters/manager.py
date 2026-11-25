"""Multi-format ReportManager and ReportBundle seam.

Encapsulates atomic unique directory claiming, multi-format generation,
in-memory rendering, failure rollback, and transparent redaction.
"""

from __future__ import annotations

import abc
import dataclasses
import io
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from codecheck.models import ReviewReport
from codecheck.redact import redact_report
from codecheck.reporters.docx_report import render_docx, write_docx_report
from codecheck.reporters.json_report import write_json_report
from codecheck.reporters.markdown_report import render_markdown, write_markdown_report
from codecheck.reporters.xlsx_report import render_xlsx, write_xlsx_report


class Reporter(abc.ABC):
    """Polymorphic reporter interface."""

    format_name: ClassVar[str]
    extension: ClassVar[str]
    content_type: ClassVar[str] = "text/plain"

    @abc.abstractmethod
    def render(self, report: ReviewReport) -> bytes | str:
        """Render the report in-memory as text or bytes."""
        ...

    def write(self, report: ReviewReport, output_path: Path) -> Path:
        """Write the report to disk at output_path."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        content = self.render(report)
        if isinstance(content, str):
            output_path.write_text(content, encoding="utf-8")
        elif isinstance(content, (bytes, bytearray)):
            output_path.write_bytes(content)
        else:
            raise TypeError(
                f"Reporter {self.format_name!r} produced invalid render type: {type(content).__name__}"
            )
        return output_path


class JsonReporter(Reporter):
    format_name = "json"
    extension = "json"
    content_type = "application/json"

    def render(self, report: ReviewReport) -> str:
        return json.dumps(report.to_dict(), indent=2)

    def write(self, report: ReviewReport, output_path: Path) -> Path:
        write_json_report(report, output_path)
        return output_path


class MarkdownReporter(Reporter):
    format_name = "markdown"
    extension = "md"
    content_type = "text/markdown; charset=utf-8"

    def render(self, report: ReviewReport) -> str:
        return render_markdown(report)

    def write(self, report: ReviewReport, output_path: Path) -> Path:
        write_markdown_report(report, output_path)
        return output_path


class DocxReporter(Reporter):
    format_name = "docx"
    extension = "docx"
    content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    def render(self, report: ReviewReport) -> bytes:
        doc = render_docx(report)
        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    def write(self, report: ReviewReport, output_path: Path) -> Path:
        write_docx_report(report, output_path)
        return output_path


class XlsxReporter(Reporter):
    format_name = "xlsx"
    extension = "xlsx"
    content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def render(self, report: ReviewReport) -> bytes:
        wb = render_xlsx(report)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def write(self, report: ReviewReport, output_path: Path) -> Path:
        write_xlsx_report(report, output_path)
        return output_path


@dataclasses.dataclass(frozen=True)
class ReportBundle:
    """An immutable bundle of generated reports for a single run."""

    run_dir: Path
    basename: str
    artifacts: dict[str, Path]
    redacted: bool

    def __getitem__(self, format_name: str) -> Path:
        if format_name not in self.artifacts:
            raise KeyError(f"Format {format_name!r} not in bundle. Available: {list(self.artifacts)}")
        return self.artifacts[format_name]

    def get(self, format_name: str, default: Path | None = None) -> Path | None:
        return self.artifacts.get(format_name, default)

    @property
    def json_path(self) -> Path | None:
        return self.artifacts.get("json")

    @property
    def markdown_path(self) -> Path | None:
        return self.artifacts.get("markdown")

    @property
    def docx_path(self) -> Path | None:
        return self.artifacts.get("docx")

    @property
    def xlsx_path(self) -> Path | None:
        return self.artifacts.get("xlsx")


class ReportGenerationError(Exception):
    def __init__(self, message: str, format_name: str, original_error: BaseException, run_dir: Path):
        super().__init__(message)
        self.format_name = format_name
        self.original_error = original_error
        self.run_dir = run_dir


class ReportManager:
    """Deep module for report rendering and multi-format bundle lifecycle."""

    def __init__(
        self,
        output_dir: Path = Path("./reports"),
        reporters: dict[str, Reporter] | None = None,
    ):
        self.output_dir = Path(output_dir)
        self._reporters: dict[str, Reporter] = {}
        if reporters is not None:
            for r in reporters.values():
                self.register(r)
        else:
            self._register_default_reporters()

    def _register_default_reporters(self) -> None:
        self.register(JsonReporter())
        self.register(MarkdownReporter())
        self.register(DocxReporter())
        self.register(XlsxReporter())

    def register(self, reporter: Reporter) -> None:
        self._reporters[reporter.format_name] = reporter

    def get_reporter(self, format_name: str) -> Reporter:
        if format_name not in self._reporters:
            raise ValueError(
                f"Unknown report format {format_name!r}. Registered formats: {list(self._reporters.keys())}"
            )
        return self._reporters[format_name]

    @property
    def registered_formats(self) -> list[str]:
        return list(self._reporters.keys())

    @staticmethod
    def sanitize_slug(text: str) -> str:
        cleaned = re.sub(r"[^\w.\-]+", "_", text).strip("_.")
        return cleaned or "repo"

    def compute_basename(
        self,
        repo_label: str,
        mode: str,
        generated_at: datetime,
        pr_number: int | None = None,
    ) -> str:
        safe_label = self.sanitize_slug(repo_label)
        suffix = f"pr{pr_number}" if pr_number is not None else mode
        timestamp = generated_at.strftime("%Y%m%d_%H%M%S")
        return f"{safe_label}_{suffix}_{timestamp}"

    def claim_unique_run_dir(self, basename: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        candidate = basename
        suffix = 2
        while True:
            run_dir = self.output_dir / candidate
            try:
                run_dir.mkdir()
                return run_dir
            except FileExistsError:
                candidate = f"{basename}-{suffix}"
                suffix += 1

    def render(
        self,
        report: ReviewReport,
        format_name: str,
        redact: bool = False,
    ) -> bytes | str:
        reporter = self.get_reporter(format_name)
        report_to_use = redact_report(report) if redact else report
        return reporter.render(report_to_use)

    def write_bundle(
        self,
        report: ReviewReport,
        repo_label: str,
        pr_number: int | None = None,
        formats: list[str] | None = None,
        redact: bool = False,
    ) -> ReportBundle:
        target_formats = formats or ["json", "markdown", "docx", "xlsx"]
        for fmt in target_formats:
            if fmt not in self._reporters:
                raise ValueError(
                    f"Unknown format {fmt!r}. Registered: {self.registered_formats}"
                )

        report_to_write = redact_report(report) if redact else report
        basename = self.compute_basename(
            repo_label=repo_label,
            mode=report.mode,
            generated_at=report.generated_at,
            pr_number=pr_number,
        )

        run_dir = self.claim_unique_run_dir(basename)
        artifacts: dict[str, Path] = {}

        try:
            for fmt in target_formats:
                reporter = self._reporters[fmt]
                target_path = run_dir / f"{run_dir.name}.{reporter.extension}"
                reporter.write(report_to_write, target_path)
                artifacts[fmt] = target_path
        except BaseException as exc:
            shutil.rmtree(run_dir, ignore_errors=True)
            if isinstance(exc, Exception):
                raise ReportGenerationError(
                    message=f"Report bundle generation failed on {fmt!r}: {exc}",
                    format_name=fmt,
                    original_error=exc,
                    run_dir=run_dir,
                ) from exc
            raise

        return ReportBundle(
            run_dir=run_dir,
            basename=run_dir.name,
            artifacts=artifacts,
            redacted=redact,
        )
