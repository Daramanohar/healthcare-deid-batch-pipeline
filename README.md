# Origin Medical - Data Operations Role Challenge

**Task 1: Containerised DICOM and PDF De-identification Pipeline**

Repository: https://github.com/Daramanohar/healthcare-deid-batch-pipeline

---

## What This Does

This pipeline ingests raw DICOM medical images and PDF clinical reports, removes patient-identifying information, writes sanitized output files, records safe metadata in a SQLite database, and produces structured logs and a run summary for every batch. It is designed to run as a containerised batch job against any folder of valid DICOM and text-based PDF files.

This repository contains the source pipeline for evaluator unit testing; before submission, the same pipeline was verified on the provided dataset of 480 DICOM instances and 50 PDF reports, with reproducible outputs generated locally under `outputs/task1_pipeline/`.

Note: raw input folders and generated output folders are intentionally excluded from GitHub to avoid publishing healthcare data. Evaluators can reproduce the outputs by placing their test files in `DICOMs/` and `PDFs/` and running `docker-compose run pipeline`.

---

## How to Run

```bash
# Step 1 - Clone the repository
git clone https://github.com/Daramanohar/healthcare-deid-batch-pipeline.git
cd healthcare-deid-batch-pipeline

# Step 2 - Add your input data
# Place DICOM files in:  DICOMs/
# Place PDF files in:    PDFs/

# Step 3 - Run smoke test first, two files per type
docker-compose run pipeline-smoke

# Step 4 - Run full pipeline
docker-compose run pipeline

# Step 5 - Validate outputs
docker-compose run validator
```

---

## Expected Outputs

After the pipeline runs, results are written here:

| What | Where |
|---|---|
| De-identified DICOM files | `outputs/task1_pipeline/sanitized/dicoms/` |
| Redacted PDF files | `outputs/task1_pipeline/sanitized/pdfs/` |
| SQLite audit database | `outputs/task1_pipeline/metadata/pipeline.db` |
| Per-file CSV manifest | `outputs/task1_pipeline/metadata/manifest_<run_id>.csv` |
| Run summary | `outputs/task1_pipeline/metadata/run_summary_<run_id>.json` |
| Structured logs | `outputs/task1_pipeline/logs/run_<run_id>.jsonl` |

The final submission package should include this source-code repository and the separate Appendix A PDF report. Generated pipeline outputs are reproducible runtime artifacts and are kept out of Git.

---

## Validation Evidence

Before submission, this pipeline was executed on the provided challenge data and produced 480 sanitized DICOM outputs, 50 redacted PDF outputs, a populated SQLite audit database, a manifest CSV, a run summary JSON, and structured JSONL logs under `outputs/task1_pipeline/`.

These generated outputs are intentionally not committed to GitHub because they are healthcare data artifacts, but they can be regenerated with `docker-compose run pipeline` or reviewed from the separately submitted/local output folder if required.

---

## Environment Variables

No configuration is required to run. Defaults work out of the box. To override locally, copy `.env.example` to `.env` and edit values.

| Variable | Default | Description |
|---|---|---|
| `PIPELINE_ENV` | `production` | Runtime environment label |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `INPUT_DICOM_DIR` | `DICOMs` | Folder containing raw DICOM files |
| `INPUT_PDF_DIR` | `PDFs` | Folder containing raw PDF reports |
| `OUTPUT_DIR` | `outputs/task1_pipeline` | Base output directory |
| `DATABASE_PATH` | `outputs/task1_pipeline/metadata/pipeline.db` | SQLite database path |

---

## PHI Removed

DICOM fields cleared or anonymized include `PatientName`, `PatientID`, `PatientBirthDate`, `PatientAge`, `PatientSex`, `PatientAddress`, `PatientTelephoneNumbers`, `ReferringPhysicianName`, `InstitutionName`, `InstitutionAddress`, `StudyID`, `AccessionNumber`, date/time values, private tags, and DICOM person-name fields.

DICOM fields preserved as safe operational/image metadata include `Modality`, `Manufacturer`, `ManufacturerModelName`, `BodyPartExamined`, `Rows`, `Columns`, `PixelSpacing`, `SliceThickness`, and related image/device descriptors. Safe patient-characteristic metadata such as age/sex is captured for audit before the sanitized DICOM output is written.

PDF redaction targets patient name, patient ID, MRN, date of birth, accession number, phone numbers, email addresses, and date patterns in selectable text PDFs.

---

## Design Decisions

SQLite is used for audit storage because it is portable and requires no separate database service. Output filenames are hashed to prevent PHI leakage through filenames. DICOM UIDs are remapped to preserve internal consistency while preventing original UID exposure. All paths are configuration-driven, and the pipeline does not assume fixed filenames, fixed file counts, or mandatory optional DICOM tags.

---

## Git Workflow

Trunk-based development with short-lived feature branches and pull requests is recommended. This keeps integration frequent, supports mandatory code review for privacy-sensitive de-identification logic, and maintains a clear audit trail for every change to PHI handling behavior.

---

## Known Limitations and Future Work

PDF de-identification covers selectable text only. Scanned PDFs would require OCR before redaction. DICOM pixel data is not scanned for burned-in annotations in this version. Both are documented as production hardening steps.
