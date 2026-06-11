from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import fitz
    import pydicom
except ModuleNotFoundError as exc:
    missing_package = exc.name
    print(
        f"Missing Python dependency: {missing_package}. "
        "Install project dependencies with `python -m pip install -r requirements.txt`, "
        "or run validation inside the Docker image.",
        file=sys.stderr,
    )
    sys.exit(2)


DEFAULT_SENSITIVE_FIELDS = (
    "Patient ID",
    "Patient Name",
    "MRN",
    "Medical Record Number",
    "Date of Birth",
    "DOB",
    "Accession Number",
)

FIELD_BOUNDARIES = (
    "Patient ID",
    "Patient Name",
    "Patient Age",
    "Gender",
    "GA",
    "BMI",
    "MRN",
    "Medical Record Number",
    "Date of Birth",
    "DOB",
    "Accession Number",
)


@dataclass(frozen=True)
class ValidationContext:
    root_dir: Path
    output_dir: Path
    database_path: Path
    run_id: str
    manifest_path: Path
    expected_dicoms: int | None
    expected_pdfs: int | None


class ValidationFailure(Exception):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Task 1 DICOM/PDF de-identification outputs.")
    parser.add_argument("--root-dir", default=".", help="Project root containing DICOMs/, PDFs/, and outputs/.")
    parser.add_argument("--output-dir", default="outputs/task1_pipeline", help="Pipeline output directory to validate.")
    parser.add_argument("--run-id", default=None, help="Run ID to validate. Defaults to the latest manifest in the selected output directory.")
    parser.add_argument("--expected-dicoms", type=int, default=None, help="Expected number of successful DICOM outputs.")
    parser.add_argument("--expected-pdfs", type=int, default=None, help="Expected number of successful PDF outputs.")
    parser.add_argument("--sample-limit", type=int, default=None, help="Optional maximum number of manifest rows to validate.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root_dir = Path(args.root_dir).resolve()
    output_dir = resolve_path(args.output_dir, root_dir)
    database_path = output_dir / "metadata" / "pipeline.db"

    try:
        run_id = args.run_id or latest_run_id_from_output(output_dir)
        manifest_path = output_dir / "metadata" / f"manifest_{run_id}.csv"
        context = ValidationContext(
            root_dir=root_dir,
            output_dir=output_dir,
            database_path=database_path,
            run_id=run_id,
            manifest_path=manifest_path,
            expected_dicoms=args.expected_dicoms,
            expected_pdfs=args.expected_pdfs,
        )
        validate(context, sample_limit=args.sample_limit)
    except ValidationFailure as exc:
        print(f"FAIL: {exc}")
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 2

    return 0


def validate(context: ValidationContext, sample_limit: int | None = None) -> None:
    print(f"Validating Task 1 output directory: {context.output_dir}")
    print(f"Run ID: {context.run_id}")

    require_file(context.database_path, "SQLite database")
    require_file(context.manifest_path, "CSV manifest")

    summary_path = context.output_dir / "metadata" / f"run_summary_{context.run_id}.json"
    require_file(summary_path, "JSON run summary")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("status") != "success":
        raise ValidationFailure(f"Run summary status is not success: {summary.get('status')}")

    db_counts = validate_database(context)
    manifest_rows = read_manifest_rows(context.manifest_path)
    if sample_limit is not None:
        manifest_rows = manifest_rows[:sample_limit]

    validate_counts(context, db_counts)
    validate_output_files_exist(context, manifest_rows)
    validate_dicom_outputs(context, manifest_rows)
    validate_pdf_outputs(context, manifest_rows)

    print("PASS: database run status is successful")
    print("PASS: manifest and summary files are present")
    print("PASS: output file counts match expectations")
    print("PASS: DICOM identifying fields are anonymized or cleared")
    print("PASS: PDF sensitive patient ID/name values are not extractable")
    print("Validation complete.")


def validate_database(context: ValidationContext) -> dict[tuple[str, str], int]:
    connection = sqlite3.connect(context.database_path)
    try:
        run = connection.execute("SELECT status FROM runs WHERE run_id = ?", (context.run_id,)).fetchone()
        if run is None:
            raise ValidationFailure(f"Run ID not found in database: {context.run_id}")
        if run[0] != "success":
            raise ValidationFailure(f"Database run status is not success: {run[0]}")

        rows = connection.execute(
            """
            SELECT source_type, status, COUNT(*)
            FROM files
            WHERE run_id = ?
            GROUP BY source_type, status
            """,
            (context.run_id,),
        ).fetchall()
    finally:
        connection.close()

    return {(source_type, status): int(count) for source_type, status, count in rows}


def validate_counts(context: ValidationContext, db_counts: dict[tuple[str, str], int]) -> None:
    dicom_count = db_counts.get(("dicom", "success"), 0)
    pdf_count = db_counts.get(("pdf", "success"), 0)
    failed_count = sum(count for (_, status), count in db_counts.items() if status != "success")

    if failed_count:
        raise ValidationFailure(f"Database contains failed files for run {context.run_id}: {failed_count}")
    if context.expected_dicoms is not None and dicom_count != context.expected_dicoms:
        raise ValidationFailure(f"Expected {context.expected_dicoms} DICOM successes but found {dicom_count}")
    if context.expected_pdfs is not None and pdf_count != context.expected_pdfs:
        raise ValidationFailure(f"Expected {context.expected_pdfs} PDF successes but found {pdf_count}")

    dicom_files = list((context.output_dir / "sanitized" / "dicoms").glob("*.dcm"))
    pdf_files = list((context.output_dir / "sanitized" / "pdfs").glob("*.pdf"))

    if len(dicom_files) < dicom_count:
        raise ValidationFailure(f"Only {len(dicom_files)} sanitized DICOM files found for {dicom_count} database successes")
    if len(pdf_files) < pdf_count:
        raise ValidationFailure(f"Only {len(pdf_files)} sanitized PDF files found for {pdf_count} database successes")


def validate_output_files_exist(context: ValidationContext, rows: list[dict[str, str]]) -> None:
    missing = []
    for row in rows:
        if row.get("status") != "success":
            continue
        output_path = resolve_record_path(row.get("output_path", ""), context.root_dir)
        if not output_path.exists():
            missing.append(str(output_path))
    if missing:
        raise ValidationFailure(f"Missing output file(s), first examples: {missing[:5]}")


def validate_dicom_outputs(context: ValidationContext, rows: list[dict[str, str]]) -> None:
    issues: list[str] = []
    filename_name_leaks: list[str] = []
    for row in rows:
        if row.get("source_type") != "dicom" or row.get("status") != "success":
            continue
        source_path = resolve_record_path(row.get("source_path", ""), context.root_dir)
        output_path = resolve_record_path(row.get("output_path", ""), context.root_dir)
        dataset = pydicom.dcmread(str(output_path), stop_before_pixels=True, force=True)

        expected_checks = {
            "PatientName": str(getattr(dataset, "PatientName", "")) == "ANONYMIZED",
            "PatientID": str(getattr(dataset, "PatientID", "")).startswith("PATIENT_"),
            "PatientAge": str(getattr(dataset, "PatientAge", "")) == "",
            "PatientBirthDate": str(getattr(dataset, "PatientBirthDate", "")) == "",
            "PatientSex": str(getattr(dataset, "PatientSex", "")) == "",
            "StudyDate": str(getattr(dataset, "StudyDate", "")) == "",
            "AccessionNumber": str(getattr(dataset, "AccessionNumber", "")) == "",
            "InstitutionName": str(getattr(dataset, "InstitutionName", "")) == "",
            "ReferringPhysicianName": str(getattr(dataset, "ReferringPhysicianName", "")) in {"", "ANONYMIZED"},
            "PatientIdentityRemoved": str(getattr(dataset, "PatientIdentityRemoved", "")) == "YES",
        }
        for key, ok in expected_checks.items():
            if not ok:
                issues.append(f"{output_path.name}: {key}={getattr(dataset, key, None)!r}")

        name_match = re.search(r"_for_([A-Za-z]+)_([A-Za-z]+)-", source_path.name)
        if name_match:
            first, last = name_match.groups()
            values = []
            for element in dataset.iterall():
                if element.VR in {"OB", "OW", "OF", "SQ", "UN"}:
                    continue
                values.append(str(element.value))
            haystack = " ".join(values)
            if first in haystack or last in haystack:
                filename_name_leaks.append(f"{source_path.name} -> {output_path.name}")

    if issues:
        raise ValidationFailure(f"DICOM de-identification issues found: {issues[:5]}")
    if filename_name_leaks:
        raise ValidationFailure(f"DICOM source filename names leaked into output metadata: {filename_name_leaks[:5]}")


def validate_pdf_outputs(context: ValidationContext, rows: list[dict[str, str]]) -> None:
    leaks: list[str] = []
    for row in rows:
        if row.get("source_type") != "pdf" or row.get("status") != "success":
            continue
        source_path = resolve_record_path(row.get("source_path", ""), context.root_dir)
        output_path = resolve_record_path(row.get("output_path", ""), context.root_dir)
        sensitive_values = targeted_pdf_values(source_path)
        output_text = extract_pdf_text(output_path)
        for value in sensitive_values:
            if value and value in output_text:
                leaks.append(f"{source_path.name}: leaked {value!r} in {output_path.name}")
    if leaks:
        raise ValidationFailure(f"PDF sensitive values still extractable: {leaks[:5]}")


def targeted_pdf_values(pdf_path: Path) -> list[str]:
    values: list[str] = []
    document = fitz.open(str(pdf_path))
    try:
        sensitive_fields = tuple(tuple(field.lower().split()) for field in DEFAULT_SENSITIVE_FIELDS)
        boundary_fields = tuple(tuple(field.lower().split()) for field in FIELD_BOUNDARIES)
        for page in document:
            lines = group_words_by_line(page.get_text("words"))
            for line in lines:
                matches = find_field_matches(line, sensitive_fields)
                boundaries = find_field_matches(line, boundary_fields)
                for start, end in matches:
                    colon_index = end
                    if colon_index < len(line) and line[colon_index]["text"] == ":":
                        value_start_x = line[colon_index]["x1"]
                    else:
                        value_start_x = line[end - 1]["x1"]
                    next_boundaries = [boundary_start for boundary_start, _ in boundaries if boundary_start > start]
                    value_stop_x = line[min(next_boundaries)]["x0"] if next_boundaries else page.rect.width
                    for word in line:
                        if word["x0"] > value_start_x and word["x1"] <= value_stop_x:
                            token = word["text"].strip()
                            if token:
                                values.append(token)
    finally:
        document.close()
    return values


def extract_pdf_text(pdf_path: Path) -> str:
    document = fitz.open(str(pdf_path))
    try:
        return "\n".join(page.get_text() for page in document)
    finally:
        document.close()


def group_words_by_line(words: list[tuple[Any, ...]], tolerance: float = 3.0) -> list[list[dict[str, Any]]]:
    lines: list[list[dict[str, Any]]] = []
    for raw_word in sorted(words, key=lambda word: ((word[1] + word[3]) / 2, word[0])):
        x0, y0, x1, y1, text, *_ = raw_word
        word = {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": text}
        center_y = (y0 + y1) / 2
        for line in lines:
            line_center = sum((item["y0"] + item["y1"]) / 2 for item in line) / len(line)
            if abs(center_y - line_center) <= tolerance:
                line.append(word)
                break
        else:
            lines.append([word])
    for line in lines:
        line.sort(key=lambda item: item["x0"])
    return lines


def find_field_matches(line: list[dict[str, Any]], fields: tuple[tuple[str, ...], ...]) -> list[tuple[int, int]]:
    normalized = [normalize_token(word["text"]) for word in line]
    matches: list[tuple[int, int]] = []
    for idx in range(len(normalized)):
        for field_tokens in fields:
            end = idx + len(field_tokens)
            if tuple(normalized[idx:end]) == field_tokens:
                matches.append((idx, end))
    return sorted(set(matches), key=lambda item: item[0])


def normalize_token(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def read_manifest_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def latest_run_id_from_output(output_dir: Path) -> str:
    metadata_dir = output_dir / "metadata"
    manifests = sorted(metadata_dir.glob("manifest_*.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not manifests:
        raise ValidationFailure(f"No manifest_*.csv files found in output metadata directory: {metadata_dir}")
    latest = manifests[0]
    return latest.stem.removeprefix("manifest_")


def require_file(path: Path, label: str) -> None:
    if not path.exists():
        raise ValidationFailure(f"{label} not found: {path}")


def resolve_path(value: str | Path, root_dir: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (root_dir / path).resolve()


def resolve_record_path(value: str, root_dir: Path) -> Path:
    if value.startswith("/workspace/"):
        return (root_dir / value.removeprefix("/workspace/")).resolve()
    windows_project_marker = "Origin-medical_labs-RC\\"
    if windows_project_marker in value:
        relative = value.split(windows_project_marker, 1)[1].replace("\\", "/")
        return (root_dir / relative).resolve()
    if re.match(r"^[A-Za-z]:\\", value):
        return Path(value)
    return resolve_path(value, root_dir)


if __name__ == "__main__":
    sys.exit(main())
