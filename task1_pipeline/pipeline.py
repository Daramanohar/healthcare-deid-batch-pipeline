from __future__ import annotations

import csv
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .config import PipelineConfig
from .db import PipelineDatabase
from .dicom_deidentifier import DicomDeidentifier
from .logging_utils import configure_logging, log_extra
from .pdf_deidentifier import PdfDeidentifier
from .utils import safe_output_name, sha256_file

LOGGER = logging.getLogger(__name__)


class BatchPipeline:
    def __init__(self, config: PipelineConfig, limit: int | None = None, limit_per_type: int | None = None) -> None:
        self.config = config
        self.limit = limit
        self.limit_per_type = limit_per_type
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
        self.log_path = configure_logging(config.output.logs_dir, self.run_id, config.logging.level)
        self.db = PipelineDatabase(config.database.path)
        self.dicom_deidentifier = DicomDeidentifier(config.dicom)
        self.pdf_deidentifier = PdfDeidentifier(config.pdf)

    def run(self) -> dict[str, object]:
        self._prepare_outputs()
        self._validate_inputs()
        self.db.create_run(
            self.run_id,
            self.config.input.dicom_dir,
            self.config.input.pdf_dir,
            self.config.output.base_dir,
            self.log_path,
        )
        log_extra(LOGGER, logging.INFO, "Pipeline run started", run_id=self.run_id)

        status = "success"
        try:
            files = self._discover_files()
            for source_type, source_path in files:
                self._process_one(source_type, source_path)
        except Exception:
            status = "failed"
            LOGGER.exception("Pipeline run failed")
            raise
        finally:
            summary = self.db.summarize_run(self.run_id)
            if status == "success" and self._failed_file_count(summary):
                status = "completed_with_errors"
            summary.update(
                {
                    "status": status,
                    "output_dir": str(self.config.output.base_dir),
                    "database_path": str(self.config.database.path),
                    "log_path": str(self.log_path),
                }
            )
            self._write_run_summary(summary)
            self._write_manifest_csv()
            self.db.finish_run(self.run_id, status, summary)
            self.db.close()
            log_extra(LOGGER, logging.INFO, "Pipeline run finished", **summary)
        return summary

    def _prepare_outputs(self) -> None:
        for path in (
            self.config.output.base_dir,
            self.config.output.sanitized_dicom_dir,
            self.config.output.sanitized_pdf_dir,
            self.config.output.metadata_dir,
            self.config.output.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def _validate_inputs(self) -> None:
        missing = [
            str(path)
            for path in (self.config.input.dicom_dir, self.config.input.pdf_dir)
            if not path.exists()
        ]
        if missing:
            raise FileNotFoundError(
                "Input folder(s) not found: "
                + ", ".join(missing)
                + ". If running in Docker, mount the project folder and set the working directory to /workspace."
            )

    def _discover_files(self) -> list[tuple[str, Path]]:
        dicom_files = sorted(self.config.input.dicom_dir.rglob("*.dcm"))
        pdf_files = sorted(self.config.input.pdf_dir.rglob("*.pdf"))
        if self.limit_per_type is not None:
            dicom_files = dicom_files[: self.limit_per_type]
            pdf_files = pdf_files[: self.limit_per_type]
        files = [("dicom", path) for path in dicom_files] + [("pdf", path) for path in pdf_files]
        if self.limit is not None:
            files = files[: self.limit]
        log_extra(
            LOGGER,
            logging.INFO,
            "Discovered input files",
            dicom_count=len(dicom_files),
            pdf_count=len(pdf_files),
            selected_count=len(files),
            limit=self.limit,
            limit_per_type=self.limit_per_type,
        )
        return files

    def _process_one(self, source_type: str, source_path: Path) -> None:
        started = time.perf_counter()
        input_sha = sha256_file(source_path)
        file_id = self.db.start_file(self.run_id, source_type, source_path, input_sha)
        output_path: Path | None = None
        try:
            if source_type == "dicom":
                output_path = self.config.output.sanitized_dicom_dir / safe_output_name("dicom", source_path, ".dcm")
                result = self.dicom_deidentifier.deidentify(source_path, output_path, input_sha)
            elif source_type == "pdf":
                output_path = self.config.output.sanitized_pdf_dir / safe_output_name("report", source_path, ".pdf")
                result = self.pdf_deidentifier.deidentify(source_path, output_path)
            else:
                raise ValueError(f"Unsupported source type: {source_type}")

            duration_ms = int((time.perf_counter() - started) * 1000)
            output_sha = sha256_file(result.output_path)
            self.db.finish_file(
                file_id,
                status="success",
                output_path=result.output_path,
                output_sha256=output_sha,
                duration_ms=duration_ms,
                metadata=result.metadata,
                warnings=result.warnings,
            )
            log_extra(
                LOGGER,
                logging.INFO,
                "Processed file",
                source_type=source_type,
                source_path=str(source_path),
                output_path=str(result.output_path),
                duration_ms=duration_ms,
                warning_count=len(result.warnings),
            )
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            self.db.finish_file(
                file_id,
                status="failed",
                output_path=output_path,
                duration_ms=duration_ms,
                error=str(exc),
            )
            log_extra(
                LOGGER,
                logging.ERROR,
                "Failed file",
                source_type=source_type,
                source_path=str(source_path),
                duration_ms=duration_ms,
                error=str(exc),
            )

    def _write_run_summary(self, summary: dict[str, object]) -> None:
        path = self.config.output.metadata_dir / f"run_summary_{self.run_id}.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, sort_keys=True)

    def _write_manifest_csv(self) -> None:
        rows = self.db.connection.execute(
            """
            SELECT source_type, source_path, output_path, input_sha256, output_sha256,
                   status, duration_ms, warnings_json, error
            FROM files
            WHERE run_id = ?
            ORDER BY id
            """,
            (self.run_id,),
        ).fetchall()
        path = self.config.output.metadata_dir / f"manifest_{self.run_id}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "source_type",
                    "source_path",
                    "output_path",
                    "input_sha256",
                    "output_sha256",
                    "status",
                    "duration_ms",
                    "warnings_json",
                    "error",
                ]
            )
            for row in rows:
                writer.writerow([row[column] for column in row.keys()])

    @staticmethod
    def _failed_file_count(summary: dict[str, object]) -> int:
        counts = summary.get("counts", {})
        if not isinstance(counts, dict):
            return 0
        failed = 0
        for source_counts in counts.values():
            if not isinstance(source_counts, dict):
                continue
            for status, count in source_counts.items():
                if status != "success":
                    failed += int(count)
        return failed
