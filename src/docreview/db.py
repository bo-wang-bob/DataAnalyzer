from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import BlockMatch, ContentBlock


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    source_path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    file_type TEXT NOT NULL,
    extension TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    page_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    x0 REAL NOT NULL,
    y0 REAL NOT NULL,
    x1 REAL NOT NULL,
    y1 REAL NOT NULL,
    confidence REAL NOT NULL,
    page_image TEXT NOT NULL,
    source_detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    block_id INTEGER NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    keyword TEXT NOT NULL,
    matched_text TEXT NOT NULL,
    match_start INTEGER NOT NULL,
    match_end INTEGER NOT NULL,
    crop_path TEXT NOT NULL,
    annotated_path TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT '待审核',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_document ON blocks(document_id);
CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(review_status);
CREATE INDEX IF NOT EXISTS idx_matches_keyword ON matches(keyword);
"""


class ReviewDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def start_document(
        self,
        document_id: str,
        sha256: str,
        source_path: Path,
        file_type: str,
        status: str = "processing",
        message: str = "",
    ) -> None:
        now = _utc_now()
        source = str(source_path.resolve())
        with self.connect() as conn:
            old = conn.execute(
                "SELECT id FROM documents WHERE source_path = ?", (source,)
            ).fetchone()
            if old:
                conn.execute("DELETE FROM documents WHERE id = ?", (old["id"],))
            conn.execute(
                """
                INSERT INTO documents
                (id, sha256, source_path, filename, file_type, extension, status, message, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    sha256,
                    source,
                    source_path.name,
                    file_type,
                    source_path.suffix.lower(),
                    status,
                    message,
                    now,
                ),
            )

    def finish_document(
        self, document_id: str, status: str, page_count: int, message: str = ""
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE documents
                SET status = ?, page_count = ?, message = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, page_count, message, _utc_now(), document_id),
            )

    def insert_block(self, document_id: str, block: ContentBlock) -> int:
        box = block.bbox.clamped()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO blocks
                (document_id, page_no, kind, text, x0, y0, x1, y1,
                 confidence, page_image, source_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    block.page_no,
                    block.kind,
                    block.text,
                    box.x0,
                    box.y0,
                    box.x1,
                    box.y1,
                    block.confidence,
                    str(block.page_image),
                    block.source_detail,
                ),
            )
            return int(cursor.lastrowid)

    def insert_match(
        self,
        block_id: int,
        match: BlockMatch,
        crop_path: Path,
        annotated_path: Path,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO matches
                (block_id, keyword, matched_text, match_start, match_end,
                 crop_path, annotated_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    block_id,
                    match.keyword,
                    match.matched_text,
                    match.start,
                    match.end,
                    str(crop_path),
                    str(annotated_path),
                    _utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def list_matches(self, limit: int = 500) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT m.id, m.keyword, m.matched_text, m.review_status, m.note,
                       m.crop_path, m.annotated_path,
                       b.page_no, b.kind, b.text, b.confidence, b.source_detail,
                       b.x0, b.y0, b.x1, b.y1,
                       d.id AS document_id, d.filename, d.source_path, d.file_type,
                       d.sha256
                FROM matches m
                JOIN blocks b ON b.id = m.block_id
                JOIN documents d ON d.id = b.document_id
                ORDER BY m.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_match(self, match_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT m.*, b.page_no, b.kind, b.text, b.confidence, b.source_detail,
                       b.x0, b.y0, b.x1, b.y1,
                       d.filename, d.source_path, d.file_type, d.sha256
                FROM matches m
                JOIN blocks b ON b.id = m.block_id
                JOIN documents d ON d.id = b.document_id
                WHERE m.id = ?
                """,
                (match_id,),
            ).fetchone()
            return dict(row) if row else None

    def update_review(self, match_id: int, status: str, note: str) -> None:
        allowed = {"待审核", "正常", "有问题", "待确认", "误识别"}
        if status not in allowed:
            raise ValueError(f"无效审核状态: {status}")
        with self.connect() as conn:
            conn.execute(
                "UPDATE matches SET review_status = ?, note = ? WHERE id = ?",
                (status, note.strip(), match_id),
            )

    def list_documents(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM documents ORDER BY updated_at DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def list_unsupported(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM documents WHERE status = 'unsupported' ORDER BY filename"
            ).fetchall()
            return [dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            result = {
                "documents": int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]),
                "matches": int(conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]),
                "unsupported": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM documents WHERE status = 'unsupported'"
                    ).fetchone()[0]
                ),
                "errors": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM documents WHERE status = 'error'"
                    ).fetchone()[0]
                ),
            }
            for row in conn.execute(
                "SELECT review_status, COUNT(*) AS count FROM matches GROUP BY review_status"
            ):
                result[row["review_status"]] = int(row["count"])
            return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
