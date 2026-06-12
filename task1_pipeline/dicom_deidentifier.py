from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pydicom
from pydicom.dataset import Dataset
from pydicom.uid import generate_uid

from .config import DicomConfig
from .utils import scrub_text_value, stable_token

LOGGER = logging.getLogger(__name__)


REPLACE_WITH_ANON = {
    "PatientName",
    "OtherPatientNames",
}

REPLACE_WITH_ID = {
    "PatientID",
    "IssuerOfPatientID",
}

CLEAR_KEYWORDS = {
    "PatientAge",
    "PatientBirthDate",
    "PatientBirthTime",
    "PatientComments",
    "PatientMotherBirthName",
    "PatientSex",
    "PatientAddress",
    "PatientTelephoneNumbers",
    "OtherPatientIDs",
    "OtherPatientIDsSequence",
    "EthnicGroup",
    "Occupation",
    "AdditionalPatientHistory",
    "AccessionNumber",
    "InstitutionName",
    "InstitutionAddress",
    "ReferringPhysicianName",
    "ReferringPhysicianAddress",
    "ReferringPhysicianTelephoneNumbers",
    "ConsultingPhysicianName",
    "PerformingPhysicianName",
    "OperatorsName",
    "AdmittingDiagnosesDescription",
    "RequestingPhysician",
    "RequestingService",
    "RequestedProcedureDescription",
    "PerformedProcedureStepDescription",
    "ImageComments",
    "MedicalRecordLocator",
    "StudyID",
    "AdmissionID",
    "ServiceEpisodeID",
    "ScheduledProcedureStepID",
    "PerformedProcedureStepID",
    "RequestedProcedureID",
}

TEXT_KEYWORDS_TO_SCRUB = {
    "StudyDescription",
    "SeriesDescription",
    "ProtocolName",
}

SAFE_METADATA_DEFAULTS = {
    "Modality",
    "Manufacturer",
    "ManufacturerModelName",
    "DeviceSerialNumber",
    "SoftwareVersions",
    "BodyPartExamined",
    "PatientSex",
    "Rows",
    "Columns",
    "PhotometricInterpretation",
    "BitsAllocated",
    "PixelSpacing",
    "SliceThickness",
    "StudyDescription",
    "SeriesDescription",
}

IMAGE_METADATA_KEYS = {
    "BitsAllocated",
    "BodyPartExamined",
    "Columns",
    "Modality",
    "PhotometricInterpretation",
    "PixelSpacing",
    "Rows",
    "SeriesDescription",
    "SliceThickness",
    "StudyDescription",
}

DEVICE_METADATA_KEYS = {
    "DeviceSerialNumber",
    "Manufacturer",
    "ManufacturerModelName",
    "SoftwareVersions",
}

NON_PHI_PATIENT_METADATA_KEYS = {
    "PatientAge",
    "PatientSex",
}


@dataclass(frozen=True)
class DicomResult:
    output_path: Path
    metadata: dict[str, Any]
    warnings: list[str]


class DicomDeidentifier:
    def __init__(self, config: DicomConfig) -> None:
        self.config = config
        self.uid_map: dict[str, str] = {}

    def deidentify(self, source_path: Path, output_path: Path, input_sha256: str) -> DicomResult:
        warnings: list[str] = []
        dataset = pydicom.dcmread(str(source_path), force=True)
        anon_patient_id = f"PATIENT_{stable_token(input_sha256, 12).upper()}"

        metadata_before = self._extract_metadata(dataset)
        non_phi_patient_metadata = self._metadata_group(dataset, NON_PHI_PATIENT_METADATA_KEYS)

        if self.config.remove_private_tags:
            dataset.remove_private_tags()

        self._deidentify_dataset(dataset, anon_patient_id, warnings)
        if self.config.pixel_redaction_enabled:
            self._redact_pixel_zones(dataset, warnings)
        self._sync_file_meta(dataset)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        dataset.save_as(str(output_path), enforce_file_format=True)

        metadata_after = self._extract_metadata(dataset)
        metadata_after["image_metadata"] = self._metadata_group(dataset, IMAGE_METADATA_KEYS)
        metadata_after["device_metadata"] = self._metadata_group(dataset, DEVICE_METADATA_KEYS)
        metadata_after["non_phi_patient_metadata"] = non_phi_patient_metadata
        metadata_after["source_metadata_preview"] = metadata_before
        metadata_after["anonymized_patient_id"] = anon_patient_id
        metadata_after["private_tags_removed"] = self.config.remove_private_tags
        return DicomResult(output_path=output_path, metadata=metadata_after, warnings=warnings)

    def _deidentify_dataset(self, dataset: Dataset, anon_patient_id: str, warnings: list[str]) -> None:
        for element in list(dataset.iterall()):
            keyword = element.keyword
            if not keyword:
                continue

            if keyword in REPLACE_WITH_ANON or element.VR == "PN":
                element.value = "ANONYMIZED"
            elif keyword in REPLACE_WITH_ID:
                element.value = anon_patient_id
            elif keyword in CLEAR_KEYWORDS:
                element.value = ""
            elif keyword in TEXT_KEYWORDS_TO_SCRUB:
                scrubbed = scrub_text_value(element.value)
                if scrubbed != str(element.value):
                    warnings.append(f"Scrubbed possible PHI from {keyword}")
                element.value = scrubbed
            elif self.config.clear_dates and element.VR == "DA":
                element.value = ""
            elif self.config.clear_times and element.VR == "TM":
                element.value = ""
            elif element.VR == "DT":
                element.value = ""
            elif element.VR == "UI":
                element.value = self._map_uid(str(element.value))

        dataset.PatientName = "ANONYMIZED"
        dataset.PatientID = anon_patient_id
        dataset.PatientIdentityRemoved = "YES"
        dataset.DeidentificationMethod = "PHI tags cleared; private tags removed; UIDs remapped"

    def _redact_pixel_zones(self, dataset: Dataset, warnings: list[str]) -> None:
        if not self.config.pixel_redaction_zones:
            return
        if not hasattr(dataset, "PixelData") or not hasattr(dataset, "Rows") or not hasattr(dataset, "Columns"):
            return
        transfer_syntax = getattr(getattr(dataset, "file_meta", None), "TransferSyntaxUID", None)
        if getattr(transfer_syntax, "is_compressed", False):
            warnings.append("Skipped pixel redaction because compressed pixel data requires decompression support.")
            return

        rows = int(dataset.Rows)
        columns = int(dataset.Columns)
        samples_per_pixel = int(getattr(dataset, "SamplesPerPixel", 1))
        bits_allocated = int(getattr(dataset, "BitsAllocated", 8))
        if bits_allocated % 8:
            warnings.append("Skipped pixel redaction because bit-packed pixel data is not supported.")
            return
        bytes_per_sample = max(1, bits_allocated // 8)
        frames = int(getattr(dataset, "NumberOfFrames", 1) or 1)
        planar_configuration = int(getattr(dataset, "PlanarConfiguration", 0) or 0)
        row_stride = columns * samples_per_pixel * bytes_per_sample
        frame_stride = rows * row_stride
        expected_min_length = frame_stride * frames
        if len(dataset.PixelData) < expected_min_length:
            warnings.append("Skipped pixel redaction because PixelData length is shorter than expected.")
            return

        redacted = bytearray(dataset.PixelData)
        fill_sample = self._pixel_fill_sample_bytes(dataset, bytes_per_sample)
        applied_zones = 0

        for zone in self.config.pixel_redaction_zones:
            x0 = self._fraction_to_index(zone.get("x0", 0.0), columns)
            y0 = self._fraction_to_index(zone.get("y0", 0.0), rows)
            x1 = self._fraction_to_index(zone.get("x1", 1.0), columns)
            y1 = self._fraction_to_index(zone.get("y1", 1.0), rows)
            x0, x1 = sorted((max(0, x0), min(columns, x1)))
            y0, y1 = sorted((max(0, y0), min(rows, y1)))
            if x0 >= x1 or y0 >= y1:
                continue
            self._apply_redaction(
                redacted,
                rows,
                columns,
                samples_per_pixel,
                bytes_per_sample,
                frames,
                planar_configuration,
                y0,
                y1,
                x0,
                x1,
                fill_sample,
            )
            applied_zones += 1

        if applied_zones:
            dataset.PixelData = bytes(redacted)
            dataset.BurnedInAnnotation = "NO"
            dataset.RecognizableVisualFeatures = "NO"
            warnings.append(f"Applied pixel redaction to {applied_zones} configured burned-in annotation zone(s).")

    @staticmethod
    def _fraction_to_index(value: object, length: int) -> int:
        return int(round(float(value) * length))

    @staticmethod
    def _pixel_fill_sample_bytes(dataset: Dataset, bytes_per_sample: int) -> bytes:
        photometric = str(getattr(dataset, "PhotometricInterpretation", "")).upper()
        signed = int(getattr(dataset, "PixelRepresentation", 0) or 0) == 1
        byteorder = "little" if getattr(dataset, "is_little_endian", True) else "big"
        if photometric == "MONOCHROME1":
            bits_stored = int(getattr(dataset, "BitsStored", bytes_per_sample * 8))
            fill_value = (2 ** (bits_stored - 1)) - 1 if signed else (2**bits_stored) - 1
        else:
            fill_value = 0
        return int(fill_value).to_bytes(bytes_per_sample, byteorder=byteorder, signed=signed)

    @staticmethod
    def _apply_redaction(
        pixel_data: bytearray,
        rows: int,
        columns: int,
        samples_per_pixel: int,
        bytes_per_sample: int,
        frames: int,
        planar_configuration: int,
        y0: int,
        y1: int,
        x0: int,
        x1: int,
        fill_sample: bytes,
    ) -> None:
        sample_width = samples_per_pixel * bytes_per_sample
        row_stride = columns * sample_width
        frame_stride = rows * row_stride
        if samples_per_pixel > 1 and planar_configuration == 1:
            plane_stride = rows * columns * bytes_per_sample
            redacted_segment = fill_sample * (x1 - x0)
            for frame in range(frames):
                frame_offset = frame * frame_stride
                for sample_index in range(samples_per_pixel):
                    plane_offset = frame_offset + sample_index * plane_stride
                    for row in range(y0, y1):
                        start = plane_offset + row * columns * bytes_per_sample + x0 * bytes_per_sample
                        end = plane_offset + row * columns * bytes_per_sample + x1 * bytes_per_sample
                        pixel_data[start:end] = redacted_segment
            return

        redacted_segment = (fill_sample * samples_per_pixel) * (x1 - x0)
        for frame in range(frames):
            frame_offset = frame * frame_stride
            for row in range(y0, y1):
                start = frame_offset + row * row_stride + x0 * sample_width
                end = frame_offset + row * row_stride + x1 * sample_width
                pixel_data[start:end] = redacted_segment

    def _map_uid(self, value: str) -> str:
        if not value:
            return value
        if value not in self.uid_map:
            self.uid_map[value] = generate_uid(prefix=self.config.uid_prefix)
        return self.uid_map[value]

    def _sync_file_meta(self, dataset: Dataset) -> None:
        if not getattr(dataset, "file_meta", None):
            return
        if hasattr(dataset, "SOPInstanceUID"):
            dataset.file_meta.MediaStorageSOPInstanceUID = dataset.SOPInstanceUID
        if hasattr(dataset, "SOPClassUID"):
            dataset.file_meta.MediaStorageSOPClassUID = dataset.SOPClassUID

    def _extract_metadata(self, dataset: Dataset) -> dict[str, Any]:
        keywords = set(SAFE_METADATA_DEFAULTS)
        keywords.update(self.config.safe_metadata_keywords)
        metadata: dict[str, Any] = {}
        for keyword in sorted(keywords):
            if hasattr(dataset, keyword):
                value = getattr(dataset, keyword)
                if keyword in TEXT_KEYWORDS_TO_SCRUB:
                    value = scrub_text_value(value)
                metadata[keyword] = str(value)
        return metadata

    def _metadata_group(self, dataset: Dataset, keywords: set[str]) -> dict[str, str]:
        metadata: dict[str, str] = {}
        for keyword in sorted(keywords):
            if not hasattr(dataset, keyword):
                continue
            value = getattr(dataset, keyword)
            if keyword in TEXT_KEYWORDS_TO_SCRUB:
                value = scrub_text_value(value)
            metadata[keyword] = str(value)
        return metadata
