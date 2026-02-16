from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Sequence


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

CREATE TABLE IF NOT EXISTS hoa_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hoa_id INTEGER NOT NULL UNIQUE REFERENCES hoas(id) ON DELETE CASCADE,
    display_name TEXT,
    website_url TEXT,
    street TEXT,
    city TEXT,
    state TEXT,
    postal_code TEXT,
    country TEXT DEFAULT 'US',
    latitude REAL,
    longitude REAL,
    source TEXT DEFAULT 'manual',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _ensure_table_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    column_def: str,
) -> None:
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = {str(row["name"]) for row in cur.fetchall()}
    if column in cols:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")
    conn.commit()


def get_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(SCHEMA)
    _ensure_table_column(conn, "hoa_locations", "website_url", "TEXT")
    _ensure_table_column(conn, "hoa_locations", "boundary_geojson", "TEXT")
    return conn


def _load_geojson(raw_value: object) -> dict | None:
    if raw_value is None:
        return None
    try:
        parsed = json.loads(str(raw_value))
    except Exception:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def get_or_create_hoa(conn: sqlite3.Connection, name: str) -> int:
    cur = conn.execute("SELECT id FROM hoas WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute("INSERT INTO hoas (name) VALUES (?)", (name,))
    conn.commit()
    return int(cur.lastrowid)


def get_hoa_id(conn: sqlite3.Connection, name: str) -> int | None:
    cur = conn.execute("SELECT id FROM hoas WHERE name = ?", (name,))
    row = cur.fetchone()
    if not row:
        return None
    return int(row["id"])


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


def list_hoa_names(conn: sqlite3.Connection) -> list[str]:
    cur = conn.execute("SELECT name FROM hoas ORDER BY name COLLATE NOCASE")
    return [str(row["name"]) for row in cur.fetchall()]


def list_hoa_names_with_documents(conn: sqlite3.Connection) -> list[str]:
    cur = conn.execute(
        """
        SELECT DISTINCT h.name
        FROM hoas h
        JOIN documents d ON d.hoa_id = h.id
        ORDER BY h.name COLLATE NOCASE
        """
    )
    return [str(row["name"]) for row in cur.fetchall()]


def list_hoa_summaries(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """
        WITH doc_stats AS (
            SELECT
                hoa_id,
                COUNT(*) AS doc_count,
                COALESCE(SUM(bytes), 0) AS total_bytes,
                MAX(last_ingested) AS last_ingested
            FROM documents
            GROUP BY hoa_id
        ),
        chunk_stats AS (
            SELECT
                d.hoa_id AS hoa_id,
                COUNT(c.id) AS chunk_count
            FROM documents d
            LEFT JOIN chunks c ON c.document_id = d.id
            GROUP BY d.hoa_id
        )
        SELECT
            h.name AS hoa,
            ds.doc_count,
            ds.total_bytes,
            ds.last_ingested,
            COALESCE(cs.chunk_count, 0) AS chunk_count,
            l.website_url,
            l.city,
            l.state,
            l.latitude,
            l.longitude,
            l.boundary_geojson
        FROM hoas h
        JOIN doc_stats ds ON ds.hoa_id = h.id
        LEFT JOIN chunk_stats cs ON cs.hoa_id = h.id
        LEFT JOIN hoa_locations l ON l.hoa_id = h.id
        ORDER BY h.name COLLATE NOCASE
        """
    )
    rows = cur.fetchall()
    return [
        {
            "hoa": str(row["hoa"]),
            "doc_count": int(row["doc_count"]),
            "chunk_count": int(row["chunk_count"]),
            "total_bytes": int(row["total_bytes"]),
            "last_ingested": str(row["last_ingested"]) if row["last_ingested"] is not None else None,
            "website_url": str(row["website_url"]) if row["website_url"] is not None else None,
            "city": str(row["city"]) if row["city"] is not None else None,
            "state": str(row["state"]) if row["state"] is not None else None,
            "latitude": float(row["latitude"]) if row["latitude"] is not None else None,
            "longitude": float(row["longitude"]) if row["longitude"] is not None else None,
            "boundary_geojson": _load_geojson(row["boundary_geojson"]),
        }
        for row in rows
    ]


def get_hoa_location(conn: sqlite3.Connection, hoa_name: str) -> dict | None:
    cur = conn.execute(
        """
        SELECT
            h.name AS hoa,
            l.display_name,
            l.website_url,
            l.street,
            l.city,
            l.state,
            l.postal_code,
            l.country,
            l.latitude,
            l.longitude,
            l.boundary_geojson,
            l.source,
            l.updated_at
        FROM hoas h
        LEFT JOIN hoa_locations l ON l.hoa_id = h.id
        WHERE h.name = ?
        """,
        (hoa_name,),
    )
    row = cur.fetchone()
    if not row:
        return None
    if (
        row["latitude"] is None
        and row["longitude"] is None
        and row["city"] is None
        and row["street"] is None
        and row["website_url"] is None
        and row["boundary_geojson"] is None
    ):
        return {
            "hoa": str(row["hoa"]),
            "display_name": None,
            "website_url": None,
            "street": None,
            "city": None,
            "state": None,
            "postal_code": None,
            "country": None,
            "latitude": None,
            "longitude": None,
            "boundary_geojson": None,
            "source": None,
            "updated_at": None,
        }
    return {
        "hoa": str(row["hoa"]),
        "display_name": str(row["display_name"]) if row["display_name"] is not None else None,
        "website_url": str(row["website_url"]) if row["website_url"] is not None else None,
        "street": str(row["street"]) if row["street"] is not None else None,
        "city": str(row["city"]) if row["city"] is not None else None,
        "state": str(row["state"]) if row["state"] is not None else None,
        "postal_code": str(row["postal_code"]) if row["postal_code"] is not None else None,
        "country": str(row["country"]) if row["country"] is not None else None,
        "latitude": float(row["latitude"]) if row["latitude"] is not None else None,
        "longitude": float(row["longitude"]) if row["longitude"] is not None else None,
        "boundary_geojson": _load_geojson(row["boundary_geojson"]),
        "source": str(row["source"]) if row["source"] is not None else None,
        "updated_at": str(row["updated_at"]) if row["updated_at"] is not None else None,
    }


def upsert_hoa_location(
    conn: sqlite3.Connection,
    hoa_name: str,
    *,
    display_name: str | None = None,
    website_url: str | None = None,
    street: str | None = None,
    city: str | None = None,
    state: str | None = None,
    postal_code: str | None = None,
    country: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    boundary_geojson: str | None = None,
    source: str | None = None,
) -> None:
    hoa_id = get_or_create_hoa(conn, hoa_name)
    existing = conn.execute(
        "SELECT id FROM hoa_locations WHERE hoa_id = ?",
        (hoa_id,),
    ).fetchone()

    if existing is None:
        conn.execute(
            """
            INSERT INTO hoa_locations
                (hoa_id, display_name, website_url, street, city, state, postal_code, country, latitude, longitude, boundary_geojson, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hoa_id,
                display_name,
                website_url,
                street,
                city,
                state,
                postal_code,
                country or "US",
                latitude,
                longitude,
                boundary_geojson,
                source or "manual",
            ),
        )
        conn.commit()
        return

    conn.execute(
        """
        UPDATE hoa_locations
        SET
            display_name = COALESCE(?, display_name),
            website_url = COALESCE(?, website_url),
            street = COALESCE(?, street),
            city = COALESCE(?, city),
            state = COALESCE(?, state),
            postal_code = COALESCE(?, postal_code),
            country = COALESCE(?, country),
            latitude = COALESCE(?, latitude),
            longitude = COALESCE(?, longitude),
            boundary_geojson = COALESCE(?, boundary_geojson),
            source = COALESCE(?, source),
            updated_at = CURRENT_TIMESTAMP
        WHERE hoa_id = ?
        """,
        (
            display_name,
            website_url,
            street,
            city,
            state,
            postal_code,
            country,
            latitude,
            longitude,
            boundary_geojson,
            source,
            hoa_id,
        ),
    )
    conn.commit()


def list_hoa_locations(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """
        SELECT
            h.name AS hoa,
            l.display_name,
            l.website_url,
            l.street,
            l.city,
            l.state,
            l.postal_code,
            l.country,
            l.latitude,
            l.longitude,
            l.boundary_geojson,
            l.source,
            l.updated_at
        FROM hoas h
        JOIN documents d ON d.hoa_id = h.id
        LEFT JOIN hoa_locations l ON l.hoa_id = h.id
        GROUP BY h.id
        ORDER BY h.name COLLATE NOCASE
        """
    )
    rows = cur.fetchall()
    return [
        {
            "hoa": str(row["hoa"]),
            "display_name": str(row["display_name"]) if row["display_name"] is not None else None,
            "website_url": str(row["website_url"]) if row["website_url"] is not None else None,
            "street": str(row["street"]) if row["street"] is not None else None,
            "city": str(row["city"]) if row["city"] is not None else None,
            "state": str(row["state"]) if row["state"] is not None else None,
            "postal_code": str(row["postal_code"]) if row["postal_code"] is not None else None,
            "country": str(row["country"]) if row["country"] is not None else None,
            "latitude": float(row["latitude"]) if row["latitude"] is not None else None,
            "longitude": float(row["longitude"]) if row["longitude"] is not None else None,
            "boundary_geojson": _load_geojson(row["boundary_geojson"]),
            "source": str(row["source"]) if row["source"] is not None else None,
            "updated_at": str(row["updated_at"]) if row["updated_at"] is not None else None,
        }
        for row in rows
    ]


def get_chunk_text_for_hoa(conn: sqlite3.Connection, hoa_name: str, limit: int = 200) -> list[str]:
    cur = conn.execute(
        """
        SELECT c.text
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        JOIN hoas h ON h.id = d.hoa_id
        WHERE h.name = ?
        ORDER BY LENGTH(c.text) DESC
        LIMIT ?
        """,
        (hoa_name, limit),
    )
    return [str(row["text"]) for row in cur.fetchall() if row["text"]]


def list_documents_for_hoa(conn: sqlite3.Connection, hoa_name: str) -> list[dict]:
    cur = conn.execute(
        """
        SELECT
            d.relative_path,
            d.bytes,
            d.page_count,
            d.last_ingested,
            COUNT(c.id) AS chunk_count
        FROM documents d
        JOIN hoas h ON h.id = d.hoa_id
        LEFT JOIN chunks c ON c.document_id = d.id
        WHERE h.name = ?
        GROUP BY d.id
        ORDER BY d.relative_path COLLATE NOCASE
        """,
        (hoa_name,),
    )
    rows = cur.fetchall()
    return [
        {
            "relative_path": str(row["relative_path"]),
            "bytes": int(row["bytes"]),
            "page_count": int(row["page_count"]) if row["page_count"] is not None else None,
            "chunk_count": int(row["chunk_count"]),
            "last_ingested": str(row["last_ingested"]),
        }
        for row in rows
    ]


def list_document_chunks_for_hoa(
    conn: sqlite3.Connection,
    hoa_name: str,
    relative_path: str,
) -> list[dict]:
    cur = conn.execute(
        """
        SELECT
            c.chunk_index,
            c.start_page,
            c.end_page,
            c.text
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        JOIN hoas h ON h.id = d.hoa_id
        WHERE h.name = ? AND d.relative_path = ?
        ORDER BY c.chunk_index ASC
        """,
        (hoa_name, relative_path),
    )
    rows = cur.fetchall()
    return [
        {
            "chunk_index": int(row["chunk_index"]),
            "start_page": int(row["start_page"]) if row["start_page"] is not None else None,
            "end_page": int(row["end_page"]) if row["end_page"] is not None else None,
            "text": str(row["text"]),
        }
        for row in rows
    ]


def list_chunk_point_ids(conn: sqlite3.Connection, document_id: int) -> list[str]:
    cur = conn.execute(
        """
        SELECT qdrant_point_id
        FROM chunks
        WHERE document_id = ? AND qdrant_point_id IS NOT NULL
        """,
        (document_id,),
    )
    return [str(row["qdrant_point_id"]) for row in cur.fetchall()]


def mark_document_for_reindex(
    conn: sqlite3.Connection,
    hoa_id: int,
    relative_path: str,
) -> None:
    conn.execute(
        """
        UPDATE documents
        SET checksum = '__FAILED__', last_ingested = CURRENT_TIMESTAMP
        WHERE hoa_id = ? AND relative_path = ?
        """,
        (hoa_id, relative_path),
    )
    conn.commit()
