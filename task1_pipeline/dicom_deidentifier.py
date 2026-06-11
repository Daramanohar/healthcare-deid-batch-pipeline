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
