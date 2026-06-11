from __future__ import annotations

import hashlib
import re
from pathlib import Path


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_token(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def safe_output_name(prefix: str, source_path: Path, extension: str) -> str:
    token = stable_token(str(source_path.resolve()), 20)
    return f"{prefix}_{token}{extension}"


def scrub_text_value(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"\bfor\s+[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)+\b", "", text)
    text = re.sub(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b", "", text)
    text = re.sub(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b", "", text)
    text = re.sub(r"\b[A-Z][a-z]+_[A-Z][a-z]+\b", "", text)
    return " ".join(text.replace("_", " ").split())
