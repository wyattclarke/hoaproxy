from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Sequence


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS hoas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hoa_id INTEGER NOT NULL REFERENCES hoas(id),
    relative_path TEXT NOT NULL,
    checksum TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    page_count INTEGER,
    last_ingested TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (hoa_id, relative_path)
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    start_page INTEGER,
    end_page INTEGER,
    text TEXT NOT NULL,
    qdrant_point_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(SCHEMA)
    return conn


def get_or_create_hoa(conn: sqlite3.Connection, name: str) -> int:
    cur = conn.execute("SELECT id FROM hoas WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute("INSERT INTO hoas (name) VALUES (?)", (name,))
    conn.commit()
    return int(cur.lastrowid)


def get_document_record(
    conn: sqlite3.Connection, hoa_id: int, relative_path: str
) -> sqlite3.Row | None:
    cur = conn.execute(
        "SELECT * FROM documents WHERE hoa_id = ? AND relative_path = ?",
        (hoa_id, relative_path),
    )
    return cur.fetchone()


def upsert_document(
    conn: sqlite3.Connection,
    hoa_id: int,
    relative_path: str,
    checksum: str,
    byte_size: int,
    page_count: int | None,
) -> tuple[int, bool]:
    """Returns document_id and whether content changed."""
    record = get_document_record(conn, hoa_id, relative_path)
    if record is None:
        cur = conn.execute(
            """
            INSERT INTO documents (hoa_id, relative_path, checksum, bytes, page_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            (hoa_id, relative_path, checksum, byte_size, page_count),
        )
        conn.commit()
        return int(cur.lastrowid), True

    if record["checksum"] == checksum:
        return int(record["id"]), False

    conn.execute(
        """
        UPDATE documents
        SET checksum = ?, bytes = ?, page_count = ?, last_ingested = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (checksum, byte_size, page_count, record["id"]),
    )
    conn.commit()
    return int(record["id"]), True


def replace_chunks(
    conn: sqlite3.Connection,
    document_id: int,
    rows: Sequence[tuple[int, int | None, int | None, str, str]],
) -> None:
    conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
    conn.executemany(
        """
        INSERT INTO chunks (document_id, chunk_index, start_page, end_page, text, qdrant_point_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [(document_id, *row) for row in rows],
    )
    conn.commit()
