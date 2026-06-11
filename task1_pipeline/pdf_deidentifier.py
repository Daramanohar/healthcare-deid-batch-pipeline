from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz

from .config import PdfConfig

LOGGER = logging.getLogger(__name__)


DEFAULT_REGEX_PATTERNS = (
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    r"\b(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b",
    r"\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b",
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b",
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

PDF_NON_PHI_METADATA_FIELDS = (
    "Gender",
    "Patient Age",
    "GA",
    "BMI",
)


@dataclass(frozen=True)
class PdfResult:
    output_path: Path
    metadata: dict[str, Any]
    warnings: list[str]


class PdfDeidentifier:
    def __init__(self, config: PdfConfig) -> None:
        self.config = config
        self.sensitive_fields = tuple(tuple(field.lower().split()) for field in config.sensitive_fields)
        self.boundary_fields = tuple(tuple(field.lower().split()) for field in FIELD_BOUNDARIES)
        self.regexes = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (*DEFAULT_REGEX_PATTERNS, *config.regex_patterns))

    def deidentify(self, source_path: Path, output_path: Path) -> PdfResult:
        warnings: list[str] = []
        output_path.parent.mkdir(parents=True, exist_ok=True)

        document = fitz.open(str(source_path))
        total_redactions = 0
        total_text_chars = 0
        non_phi_patient_metadata: dict[str, str] = {}

        for page_index, page in enumerate(document):
            page_text = page.get_text()
            total_text_chars += len(page_text)
            non_phi_patient_metadata.update(self._extract_non_phi_metadata(page))
            redaction_rects = []
            redaction_rects.extend(self._rects_for_sensitive_fields(page))
            redaction_rects.extend(self._rects_for_regex_matches(page, page_text))

            if redaction_rects:
                total_redactions += len(redaction_rects)
                for rect in self._deduplicate_rects(redaction_rects):
                    page.add_redact_annot(rect, fill=(0, 0, 0))
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
                LOGGER.debug("Applied PDF redactions", extra={"extra_fields": {"page": page_index + 1, "count": len(redaction_rects)}})

        if total_text_chars == 0:
            warnings.append("No selectable text found; OCR redaction would be required for scanned-only PDFs.")

        document.save(str(output_path), garbage=4, deflate=True, clean=True)
        document.close()

        output_document = fitz.open(str(output_path))
        try:
            page_count = len(output_document)
        finally:
            output_document.close()

        metadata = {
            "pages": page_count,
            "text_chars_before_redaction": total_text_chars,
            "redactions_applied": total_redactions,
            "redaction_strategy": "field-label coordinate redaction plus regex redaction",
            "non_phi_patient_metadata": non_phi_patient_metadata,
        }
        return PdfResult(output_path=output_path, metadata=metadata, warnings=warnings)

    def _rects_for_sensitive_fields(self, page: fitz.Page) -> list[fitz.Rect]:
        lines = self._group_words_by_line(page.get_text("words"))
        rects: list[fitz.Rect] = []
        for line in lines:
            matches = self._find_field_matches(line, self.sensitive_fields)
            boundaries = self._find_field_matches(line, self.boundary_fields)
            if not matches:
                continue
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
                        rects.append(self._padded_rect(word["rect"]))
        return rects

    def _extract_non_phi_metadata(self, page: fitz.Page) -> dict[str, str]:
        fields = tuple(tuple(field.lower().split()) for field in PDF_NON_PHI_METADATA_FIELDS)
        lines = self._group_words_by_line(page.get_text("words"))
        metadata: dict[str, str] = {}
        for line in lines:
            field_matches = self._find_field_matches(line, fields)
            boundary_matches = self._find_field_matches(line, self.boundary_fields)
            for start, end in field_matches:
                field_name = " ".join(line[index]["text"] for index in range(start, end)).strip()
                colon_index = end
                if colon_index < len(line) and line[colon_index]["text"] == ":":
                    value_start_x = line[colon_index]["x1"]
                else:
                    value_start_x = line[end - 1]["x1"]
                next_boundaries = [boundary_start for boundary_start, _ in boundary_matches if boundary_start > start]
                value_stop_x = line[min(next_boundaries)]["x0"] if next_boundaries else page.rect.width
                value_tokens = [
                    word["text"].strip()
                    for word in line
                    if word["x0"] > value_start_x and word["x1"] <= value_stop_x and word["text"].strip()
                ]
                if value_tokens:
                    metadata[self._metadata_key(field_name)] = " ".join(value_tokens)
        return metadata

    def _rects_for_regex_matches(self, page: fitz.Page, page_text: str) -> list[fitz.Rect]:
        rects: list[fitz.Rect] = []
        seen_matches: set[str] = set()
        for regex in self.regexes:
            for match in regex.finditer(page_text):
                text = match.group(0).strip()
                if not text or text in seen_matches:
                    continue
                seen_matches.add(text)
                for rect in page.search_for(text):
                    rects.append(self._padded_rect(rect))
        return rects

    def _find_field_matches(self, line: list[dict[str, Any]], fields: tuple[tuple[str, ...], ...]) -> list[tuple[int, int]]:
        normalized = [self._normalize_token(word["text"]) for word in line]
        matches: list[tuple[int, int]] = []
        for idx in range(len(normalized)):
            for field_tokens in fields:
                end = idx + len(field_tokens)
                if tuple(normalized[idx:end]) == field_tokens:
                    matches.append((idx, end))
        return sorted(set(matches), key=lambda item: item[0])

    def _group_words_by_line(self, words: list[tuple[Any, ...]], tolerance: float = 3.0) -> list[list[dict[str, Any]]]:
        lines: list[list[dict[str, Any]]] = []
        for raw_word in sorted(words, key=lambda word: ((word[1] + word[3]) / 2, word[0])):
            x0, y0, x1, y1, text, *_ = raw_word
            word = {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": text, "rect": fitz.Rect(x0, y0, x1, y1)}
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

    def _padded_rect(self, rect: fitz.Rect) -> fitz.Rect:
        pad = self.config.redaction_padding
        return fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)

    @staticmethod
    def _normalize_token(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", text.lower())

    @staticmethod
    def _metadata_key(field_name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", field_name.lower()).strip("_")

    @staticmethod
    def _deduplicate_rects(rects: list[fitz.Rect]) -> list[fitz.Rect]:
        seen: set[tuple[int, int, int, int]] = set()
        unique: list[fitz.Rect] = []
        for rect in rects:
            key = tuple(round(value) for value in (rect.x0, rect.y0, rect.x1, rect.y1))
            if key in seen:
                continue
            seen.add(key)
            unique.append(rect)
        return unique
