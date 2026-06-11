from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class InputConfig:
    dicom_dir: Path
    pdf_dir: Path


@dataclass(frozen=True)
class OutputConfig:
    base_dir: Path
    sanitized_dicom_dir: Path
    sanitized_pdf_dir: Path
    metadata_dir: Path
    logs_dir: Path


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path


@dataclass(frozen=True)
class DicomConfig:
    remove_private_tags: bool
    clear_dates: bool
    clear_times: bool
    uid_prefix: str
    safe_metadata_keywords: tuple[str, ...]


@dataclass(frozen=True)
class PdfConfig:
    sensitive_fields: tuple[str, ...]
    regex_patterns: tuple[str, ...]
    redaction_padding: float


@dataclass(frozen=True)
class LoggingConfig:
    level: str


@dataclass(frozen=True)
class PipelineConfig:
    root_dir: Path
    input: InputConfig
    output: OutputConfig
    database: DatabaseConfig
    dicom: DicomConfig
    pdf: PdfConfig
    logging: LoggingConfig


def _resolve_path(value: str | Path, root_dir: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (root_dir / path).resolve()


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_config(path: str | Path, args: argparse.Namespace | None = None) -> PipelineConfig:
    config_path = Path(path).resolve()
    raw = _read_json(config_path)
    raw_root_dir = raw.get("root_dir")
    root_dir = _resolve_path(raw_root_dir, config_path.parent.parent) if raw_root_dir else config_path.parent.parent.resolve()

    input_raw = raw.get("input", {})
    output_raw = raw.get("output", {})
    database_raw = raw.get("database", {})
    dicom_raw = raw.get("dicom", {})
    pdf_raw = raw.get("pdf", {})
    logging_raw = raw.get("logging", {})

    dicom_dir = _resolve_path(os.getenv("INPUT_DICOM_DIR", input_raw.get("dicom_dir", "DICOMs")), root_dir)
    pdf_dir = _resolve_path(os.getenv("INPUT_PDF_DIR", input_raw.get("pdf_dir", "PDFs")), root_dir)
    base_dir = _resolve_path(os.getenv("OUTPUT_DIR", output_raw.get("base_dir", "outputs/task1_pipeline")), root_dir)

    if args is not None:
        if getattr(args, "input_dicom_dir", None):
            dicom_dir = Path(args.input_dicom_dir).resolve()
        if getattr(args, "input_pdf_dir", None):
            pdf_dir = Path(args.input_pdf_dir).resolve()
        if getattr(args, "output_dir", None):
            base_dir = Path(args.output_dir).resolve()

    metadata_dir = base_dir / "metadata"
    logs_dir = base_dir / "logs"

    if args is not None:
        if getattr(args, "database_path", None):
            database_path = Path(args.database_path).resolve()
        elif getattr(args, "output_dir", None):
            database_path = base_dir / "metadata" / "pipeline.db"
        else:
            database_path = _resolve_path(os.getenv("DATABASE_PATH", database_raw.get("path", metadata_dir / "pipeline.db")), root_dir)
    else:
        database_path = _resolve_path(os.getenv("DATABASE_PATH", database_raw.get("path", metadata_dir / "pipeline.db")), root_dir)

    return PipelineConfig(
        root_dir=root_dir,
        input=InputConfig(dicom_dir=dicom_dir, pdf_dir=pdf_dir),
        output=OutputConfig(
            base_dir=base_dir,
            sanitized_dicom_dir=base_dir / "sanitized" / "dicoms",
            sanitized_pdf_dir=base_dir / "sanitized" / "pdfs",
            metadata_dir=metadata_dir,
            logs_dir=logs_dir,
        ),
        database=DatabaseConfig(path=database_path),
        dicom=DicomConfig(
            remove_private_tags=bool(dicom_raw.get("remove_private_tags", True)),
            clear_dates=bool(dicom_raw.get("clear_dates", True)),
            clear_times=bool(dicom_raw.get("clear_times", True)),
            uid_prefix=str(dicom_raw.get("uid_prefix", "2.25.")),
            safe_metadata_keywords=tuple(dicom_raw.get("safe_metadata_keywords", [])),
        ),
        pdf=PdfConfig(
            sensitive_fields=tuple(pdf_raw.get("sensitive_fields", [])),
            regex_patterns=tuple(pdf_raw.get("regex_patterns", [])),
            redaction_padding=float(pdf_raw.get("redaction_padding", 1.5)),
        ),
        logging=LoggingConfig(level=str(os.getenv("LOG_LEVEL", logging_raw.get("level", "INFO"))).upper()),
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the healthcare DICOM/PDF de-identification batch pipeline.")
    parser.add_argument("--config", default="config/pipeline_config.json", help="Path to pipeline JSON configuration.")
    parser.add_argument("--limit", type=int, default=None, help="Optional maximum number of files to process for a smoke run.")
    parser.add_argument("--limit-per-type", type=int, default=None, help="Optional maximum number of DICOMs and PDFs to process per type.")
    parser.add_argument("--input-dicom-dir", default=None, help="Override configured DICOM input directory.")
    parser.add_argument("--input-pdf-dir", default=None, help="Override configured PDF input directory.")
    parser.add_argument("--output-dir", default=None, help="Override configured output directory.")
    parser.add_argument("--database-path", default=None, help="Override configured SQLite database path.")
    return parser
