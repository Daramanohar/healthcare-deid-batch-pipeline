from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PipelineDatabase:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.connection.close()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                input_dicom_dir TEXT NOT NULL,
                input_pdf_dir TEXT NOT NULL,
                output_dir TEXT NOT NULL,
                log_path TEXT,
                summary_json TEXT
            );

            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_path TEXT NOT NULL,
                output_path TEXT,
                input_sha256 TEXT,
                output_sha256 TEXT,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_ms INTEGER,
                metadata_json TEXT,
                warnings_json TEXT,
                error TEXT,
                FOREIGN KEY(run_id) REFERENCES runs(run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_files_run_id ON files(run_id);
            CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
            CREATE INDEX IF NOT EXISTS idx_files_source_type ON files(source_type);
            """
        )
        self.connection.commit()

    def create_run(self, run_id: str, input_dicom_dir: Path, input_pdf_dir: Path, output_dir: Path, log_path: Path) -> None:
        self.connection.execute(
            """
            INSERT INTO runs(run_id, started_at, status, input_dicom_dir, input_pdf_dir, output_dir, log_path)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, utc_now(), "running", str(input_dicom_dir), str(input_pdf_dir), str(output_dir), str(log_path)),
        )
        self.connection.commit()

    def finish_run(self, run_id: str, status: str, summary: dict[str, Any]) -> None:
        self.connection.execute(
            """
            UPDATE runs SET finished_at = ?, status = ?, summary_json = ? WHERE run_id = ?
            """,
            (utc_now(), status, json.dumps(summary, ensure_ascii=True, sort_keys=True), run_id),
        )
        self.connection.commit()

    def start_file(self, run_id: str, source_type: str, source_path: Path, input_sha256: str) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO files(run_id, source_type, source_path, input_sha256, status, started_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, source_type, str(source_path), input_sha256, "running", utc_now()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_file(
        self,
        file_id: int,
        status: str,
        output_path: Path | None = None,
        output_sha256: str | None = None,
        duration_ms: int | None = None,
        metadata: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE files
            SET status = ?, output_path = ?, output_sha256 = ?, finished_at = ?, duration_ms = ?,
                metadata_json = ?, warnings_json = ?, error = ?
            WHERE id = ?
            """,
            (
                status,
                str(output_path) if output_path else None,
                output_sha256,
                utc_now(),
                duration_ms,
                json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
                json.dumps(warnings or [], ensure_ascii=True),
                error,
                file_id,
            ),
        )
        self.connection.commit()

    def summarize_run(self, run_id: str) -> dict[str, Any]:
        rows = self.connection.execute(
            """
            SELECT source_type, status, COUNT(*) AS count
            FROM files
            WHERE run_id = ?
            GROUP BY source_type, status
            ORDER BY source_type, status
            """,
            (run_id,),
        ).fetchall()
        totals: dict[str, Any] = {"run_id": run_id, "counts": {}, "total_files": 0}
        for row in rows:
            source_type = row["source_type"]
            status = row["status"]
            count = int(row["count"])
            totals["counts"].setdefault(source_type, {})[status] = count
            totals["total_files"] += count
        return totals
