from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import fitz
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create synthetic DICOM/PDF fixtures and run a black-box Task 1 smoke test."
    )
    parser.add_argument(
        "--work-dir",
        default="outputs/task1_synthetic_test",
        help="Directory where synthetic inputs and pipeline outputs will be written.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    work_dir = resolve_path(args.work_dir)
    run_dir = work_dir / datetime.now(timezone.utc).strftime("run_%Y%m%dT%H%M%SZ")
    input_dicom_dir = run_dir / "input" / "DICOMs"
    input_pdf_dir = run_dir / "input" / "PDFs"
    output_dir = run_dir / "output"

    input_dicom_dir.mkdir(parents=True, exist_ok=True)
    input_pdf_dir.mkdir(parents=True, exist_ok=True)

    create_synthetic_dicom(input_dicom_dir / "synthetic_for_Alice_Smith-001.dcm")
    create_synthetic_pdf(input_pdf_dir / "synthetic_patient_report.pdf")

    run_command(
        [
            sys.executable,
            "-m",
            "task1_pipeline",
            "--config",
            "config/pipeline_config.json",
            "--input-dicom-dir",
            str(input_dicom_dir),
            "--input-pdf-dir",
            str(input_pdf_dir),
            "--output-dir",
            str(output_dir),
        ]
    )
    run_command(
        [
            sys.executable,
            "scripts/validate_task1_outputs.py",
            "--root-dir",
            str(PROJECT_ROOT),
            "--output-dir",
            str(output_dir),
            "--expected-dicoms",
            "1",
            "--expected-pdfs",
            "1",
        ]
    )

    print(f"PASS: synthetic black-box test completed at {run_dir}")
    return 0


def create_synthetic_dicom(path: Path) -> None:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()

    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.is_little_endian = True
    dataset.is_implicit_VR = False
    dataset.SOPClassUID = file_meta.MediaStorageSOPClassUID
    dataset.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID

    dataset.PatientName = "Alice^Smith"
    dataset.PatientID = "AS-12345"
    dataset.StudyDate = "20200102"
    dataset.StudyTime = "101112"
    dataset.AccessionNumber = "ACC-999"
    dataset.InstitutionName = "Example Hospital"
    dataset.ReferringPhysicianName = "Doctor^Example"
    dataset.StudyDescription = "Chest exam for Alice Smith on 01/02/2020"
    dataset.SeriesDescription = "Follow up for AS-12345"

    dataset.Modality = "OT"
    dataset.Manufacturer = "Synthetic Scanner"
    dataset.ManufacturerModelName = "FixtureModel"
    dataset.BodyPartExamined = "CHEST"
    dataset.PatientSex = "F"
    dataset.Rows = 2
    dataset.Columns = 2
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 8
    dataset.BitsStored = 8
    dataset.HighBit = 7
    dataset.PixelRepresentation = 0
    dataset.PixelData = bytes([0, 64, 128, 255])

    # PatientBirthDate and several other optional PHI tags are deliberately absent.
    dataset.save_as(str(path), enforce_file_format=True)


def create_synthetic_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    lines = [
        "Patient Name: Alice Smith   Patient ID: AS-12345   Gender: Female   Patient Age: 31",
        "Date of Birth: 02/03/1994   Accession Number: ACC-999",
        "Email: alice.smith@example.com   Phone: 555-123-4567",
        "Clinical note: routine synthetic report text for de-identification testing.",
    ]
    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 24
    document.save(str(path))
    document.close()


def run_command(command: list[str]) -> None:
    printable = " ".join(command)
    print(f"Running: {printable}", flush=True)
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


if __name__ == "__main__":
    raise SystemExit(main())
