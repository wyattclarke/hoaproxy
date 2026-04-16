from __future__ import annotations

import gzip
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Sequence


# ---------------------------------------------------------------------------
# Slug utilities (shared between backend and used for hierarchical URLs)
# ---------------------------------------------------------------------------

def slugify_name(name: str) -> str:
    """HOA or city name → URL slug.  'Vista Point HOA Inc' → 'vista-point-hoa-inc'"""
    s = name.strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


# Alias for readability — same logic works for city names.
slugify_city = slugify_name


def build_hoa_path(name: str, city: str | None, state: str | None) -> str:
    """Build canonical URL path for an HOA page."""
    if not state or not city:
        return f"/hoa/{slugify_name(name)}"
    return f"/hoa/{state.lower()}/{slugify_city(city)}/{slugify_name(name)}"


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
    metadata_type TEXT,
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

CREATE TABLE IF NOT EXISTS legal_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jurisdiction TEXT NOT NULL,
    community_type TEXT NOT NULL,
    entity_form TEXT NOT NULL DEFAULT 'unknown',
    governing_law_bucket TEXT NOT NULL,
    source_type TEXT NOT NULL,
    publisher TEXT,
    citation TEXT NOT NULL,
    citation_url TEXT NOT NULL,
    effective_date TEXT,
    last_verified_date TEXT,
    checksum_sha256 TEXT,
    snapshot_path TEXT,
    parser_version TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (jurisdiction, community_type, entity_form, governing_law_bucket, citation, citation_url)
);

CREATE INDEX IF NOT EXISTS idx_legal_sources_scope
    ON legal_sources(jurisdiction, community_type, entity_form, governing_law_bucket);

CREATE TABLE IF NOT EXISTS legal_sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES legal_sources(id) ON DELETE CASCADE,
    section_key TEXT NOT NULL,
    heading TEXT,
    text TEXT NOT NULL,
    ordinal INTEGER NOT NULL DEFAULT 0,
    checksum_sha256 TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_id, section_key)
);

CREATE INDEX IF NOT EXISTS idx_legal_sections_source
    ON legal_sections(source_id, ordinal);

CREATE TABLE IF NOT EXISTS legal_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jurisdiction TEXT NOT NULL,
    community_type TEXT NOT NULL,
    entity_form TEXT NOT NULL DEFAULT 'unknown',
    topic_family TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    applies_to TEXT,
    value_text TEXT NOT NULL,
    value_numeric REAL,
    value_unit TEXT,
    conditions TEXT,
    exceptions TEXT,
    source_id INTEGER REFERENCES legal_sources(id) ON DELETE CASCADE,
    section_id INTEGER REFERENCES legal_sections(id) ON DELETE SET NULL,
    citation TEXT NOT NULL,
    citation_url TEXT,
    effective_date TEXT,
    last_verified_date TEXT,
    confidence TEXT NOT NULL DEFAULT 'medium',
    needs_human_review INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_legal_rules_scope
    ON legal_rules(jurisdiction, community_type, entity_form, topic_family);
CREATE INDEX IF NOT EXISTS idx_legal_rules_type
    ON legal_rules(topic_family, rule_type);

CREATE TABLE IF NOT EXISTS jurisdiction_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jurisdiction TEXT NOT NULL,
    community_type TEXT NOT NULL,
    entity_form TEXT NOT NULL DEFAULT 'unknown',
    governing_law_stack TEXT NOT NULL DEFAULT '[]',
    records_access_summary TEXT,
    records_sharing_limits_summary TEXT,
    proxy_voting_summary TEXT,
    conflict_resolution_notes TEXT,
    known_gaps TEXT NOT NULL DEFAULT '[]',
    confidence TEXT NOT NULL DEFAULT 'low',
    last_verified_date TEXT,
    source_rule_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (jurisdiction, community_type, entity_form)
);

CREATE INDEX IF NOT EXISTS idx_jurisdiction_profiles_scope
    ON jurisdiction_profiles(jurisdiction, community_type, entity_form);

CREATE TABLE IF NOT EXISTS legal_ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_phase TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    display_name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    verified_at TIMESTAMP,
    google_id TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_jti TEXT UNIQUE NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS email_verification_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS membership_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    hoa_id INTEGER NOT NULL REFERENCES hoas(id),
    unit_number TEXT,
    status TEXT DEFAULT 'self_declared',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, hoa_id)
);

CREATE TABLE IF NOT EXISTS delegates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    hoa_id INTEGER NOT NULL REFERENCES hoas(id),
    bio TEXT,
    contact_email TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, hoa_id)
);

CREATE TABLE IF NOT EXISTS proxy_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grantor_user_id INTEGER NOT NULL REFERENCES users(id),
    delegate_user_id INTEGER NOT NULL REFERENCES users(id),
    hoa_id INTEGER NOT NULL REFERENCES hoas(id),
    jurisdiction TEXT NOT NULL,
    community_type TEXT NOT NULL,
    direction TEXT DEFAULT 'directed',
    voting_instructions TEXT,
    for_meeting_date DATE,
    expires_at DATE,
    status TEXT DEFAULT 'draft',
    form_html TEXT,
    signed_pdf_path TEXT,
    signed_at TIMESTAMP,
    delivered_at TIMESTAMP,
    acknowledged_at TIMESTAMP,
    revoked_at TIMESTAMP,
    revoke_reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS proxy_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proxy_id INTEGER NOT NULL REFERENCES proxy_assignments(id),
    action TEXT NOT NULL,
    actor_user_id INTEGER REFERENCES users(id),
    details TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS participation_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hoa_id INTEGER NOT NULL REFERENCES hoas(id),
    meeting_date DATE NOT NULL,
    meeting_type TEXT,
    total_units INTEGER,
    votes_cast INTEGER,
    quorum_required INTEGER,
    quorum_met BOOLEAN,
    source_document_id INTEGER REFERENCES documents(id),
    entered_by_user_id INTEGER REFERENCES users(id),
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(hoa_id, meeting_date, meeting_type)
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hoa_id INTEGER NOT NULL REFERENCES hoas(id),
    creator_user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'Other',
    status TEXT NOT NULL DEFAULT 'private',
    share_code TEXT NOT NULL UNIQUE,
    cosigner_count INTEGER NOT NULL DEFAULT 0,
    upvote_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_proposals_hoa ON proposals(hoa_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_share_code ON proposals(share_code);

CREATE TABLE IF NOT EXISTS proposal_cosigners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(proposal_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_proposal_cosigners_proposal ON proposal_cosigners(proposal_id);

CREATE TABLE IF NOT EXISTS proposal_upvotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(proposal_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_proposal_upvotes_proposal ON proposal_upvotes(proposal_id);

CREATE TABLE IF NOT EXISTS api_usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    service TEXT NOT NULL,
    operation TEXT NOT NULL,
    units REAL NOT NULL,
    unit_type TEXT NOT NULL,
    est_cost_usd REAL,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_usage_log_ts ON api_usage_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_api_usage_log_service ON api_usage_log(service);

CREATE TABLE IF NOT EXISTS fixed_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,
    description TEXT,
    amount_usd REAL NOT NULL,
    frequency TEXT NOT NULL DEFAULT 'monthly',
    monthly_equiv REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT
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
    try:
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.executescript(SCHEMA)
    except (sqlite3.DatabaseError, Exception) as exc:
        if "malformed" in str(exc) or "not a database" in str(exc):
            import logging
            logging.getLogger(__name__).error(
                "DB malformed at %s — renaming to .corrupt and starting fresh", db_path)
            conn.close()
            corrupt = db_path.with_suffix(".db.corrupt")
            if corrupt.exists():
                corrupt.unlink()
            db_path.rename(corrupt)
            # Also remove WAL/SHM
            for suffix in (".db-wal", ".db-shm"):
                wal = db_path.parent / (db_path.stem + suffix)
                if wal.exists():
                    wal.unlink()
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.executescript(SCHEMA)
        else:
            raise
    _ensure_table_column(conn, "hoa_locations", "metadata_type", "TEXT")
    _ensure_table_column(conn, "hoa_locations", "website_url", "TEXT")
    _ensure_table_column(conn, "hoa_locations", "boundary_geojson", "TEXT")
    # M6 migrations
    _ensure_table_column(conn, "hoas", "board_email", "TEXT")
    # Proposal location fields
    _ensure_table_column(conn, "proposals", "lat", "REAL")
    _ensure_table_column(conn, "proposals", "lng", "REAL")
    _ensure_table_column(conn, "proposals", "location_description", "TEXT")
    # Proxy validity + verification
    _ensure_table_column(conn, "hoas", "proxy_status", "TEXT DEFAULT 'unknown'")
    _ensure_table_column(conn, "hoas", "proxy_citation", "TEXT")
    _ensure_table_column(conn, "proxy_assignments", "verification_code", "TEXT")
    _ensure_table_column(conn, "proxy_assignments", "form_hash", "TEXT")
    # Google OAuth
    _ensure_table_column(conn, "users", "google_id", "TEXT")
    # Add unique index for google_id (safe if column was created with UNIQUE in schema)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id)")
    # Embedding column for inline vector search (replaces Qdrant)
    _ensure_table_column(conn, "chunks", "embedding", "BLOB")
    conn.commit()
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


def _load_json_list(raw_value: object) -> list:
    if raw_value is None:
        return []
    try:
        parsed = json.loads(str(raw_value))
    except Exception:
        return []
    if isinstance(parsed, list):
        return parsed
    return []


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
    embeddings: Sequence[bytes] | None = None,
) -> None:
    conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
    if embeddings:
        conn.executemany(
            """
            INSERT INTO chunks (document_id, chunk_index, start_page, end_page, text, qdrant_point_id, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [(document_id, *row, emb) for row, emb in zip(rows, embeddings)],
        )
    else:
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


def set_hoa_board_email(conn: sqlite3.Connection, hoa_id: int, board_email: str | None) -> None:
    conn.execute(
        "UPDATE hoas SET board_email = ? WHERE id = ?",
        (board_email, int(hoa_id)),
    )
    conn.commit()


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


def list_hoa_summaries(
    conn: sqlite3.Connection,
    q: str | None = None,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    params: list[Any] = []
    where_clauses: list[str] = []

    if q:
        like = f"%{q}%"
        where_clauses.append("(h.name LIKE ? OR l.city LIKE ? OR l.state LIKE ?)")
        params.extend([like, like, like])
    if state:
        where_clauses.append("l.state = ?")
        params.append(state)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    base_query = f"""
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
            h.id AS hoa_id,
            h.name AS hoa,
            COALESCE(ds.doc_count, 0) AS doc_count,
            COALESCE(ds.total_bytes, 0) AS total_bytes,
            ds.last_ingested,
            COALESCE(cs.chunk_count, 0) AS chunk_count,
            l.metadata_type,
            l.website_url,
            l.city,
            l.state,
            l.latitude,
            l.longitude,
            l.boundary_geojson
        FROM hoas h
        LEFT JOIN doc_stats ds ON ds.hoa_id = h.id
        LEFT JOIN chunk_stats cs ON cs.hoa_id = h.id
        LEFT JOIN hoa_locations l ON l.hoa_id = h.id
        {where_sql}
        ORDER BY h.name COLLATE NOCASE
    """

    count_cur = conn.execute(
        f"SELECT COUNT(*) FROM ({base_query})", params
    )
    total = count_cur.fetchone()[0]

    cur = conn.execute(base_query + " LIMIT ? OFFSET ?", params + [limit, offset])
    rows = cur.fetchall()
    results = [
        {
            "hoa_id": int(row["hoa_id"]),
            "hoa": str(row["hoa"]),
            "doc_count": int(row["doc_count"]),
            "chunk_count": int(row["chunk_count"]),
            "total_bytes": int(row["total_bytes"]),
            "last_ingested": str(row["last_ingested"]) if row["last_ingested"] is not None else None,
            "metadata_type": str(row["metadata_type"]) if row["metadata_type"] is not None else None,
            "website_url": str(row["website_url"]) if row["website_url"] is not None else None,
            "city": str(row["city"]) if row["city"] is not None else None,
            "state": str(row["state"]) if row["state"] is not None else None,
            "latitude": float(row["latitude"]) if row["latitude"] is not None else None,
            "longitude": float(row["longitude"]) if row["longitude"] is not None else None,
            "boundary_geojson": _load_geojson(row["boundary_geojson"]),
        }
        for row in rows
    ]
    return {"results": results, "total": total}


def list_hoa_states(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """
        SELECT l.state, COUNT(DISTINCT h.id) AS count
        FROM hoas h
        JOIN hoa_locations l ON l.hoa_id = h.id
        LEFT JOIN documents d ON d.hoa_id = h.id
        WHERE l.state IS NOT NULL
        GROUP BY l.state
        ORDER BY l.state
        """
    )
    return [{"state": str(row[0]), "count": int(row[1])} for row in cur.fetchall()]


def list_hoa_map_points(
    conn: sqlite3.Connection,
    q: str | None = None,
    state: str | None = None,
) -> list[dict]:
    """Lightweight query returning only fields needed for map markers."""
    params: list[Any] = []
    where_clauses: list[str] = []
    if q:
        like = f"%{q}%"
        where_clauses.append("(h.name LIKE ? OR l.city LIKE ? OR l.state LIKE ?)")
        params.extend([like, like, like])
    if state:
        where_clauses.append("l.state = ?")
        params.append(state)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    cur = conn.execute(
        f"""
        SELECT
            h.name AS hoa,
            COALESCE(ds.doc_count, 0) AS doc_count,
            l.city,
            l.state,
            l.latitude,
            l.longitude,
            l.boundary_geojson
        FROM hoas h
        LEFT JOIN (
            SELECT hoa_id, COUNT(*) AS doc_count FROM documents GROUP BY hoa_id
        ) ds ON ds.hoa_id = h.id
        LEFT JOIN hoa_locations l ON l.hoa_id = h.id
        {where_sql}
        """,
        params,
    )
    return [
        {
            "hoa": str(row["hoa"]),
            "doc_count": int(row["doc_count"]),
            "city": str(row["city"]) if row["city"] is not None else None,
            "state": str(row["state"]) if row["state"] is not None else None,
            "latitude": float(row["latitude"]) if row["latitude"] is not None else None,
            "longitude": float(row["longitude"]) if row["longitude"] is not None else None,
            "boundary_geojson": _load_geojson(row["boundary_geojson"]),
        }
        for row in cur.fetchall()
    ]


def resolve_hoa_by_slug(conn: sqlite3.Connection, slug: str) -> dict | None:
    cur = conn.execute(
        """
        SELECT h.id AS hoa_id, h.name AS hoa_name, l.city, l.state
        FROM hoas h
        LEFT JOIN hoa_locations l ON l.hoa_id = h.id
        """
    )
    rows = cur.fetchall()

    def slugify(name: str) -> str:
        s = name.strip().lower()
        s = re.sub(r"\s+", "_", s)
        s = re.sub(r"[^a-z0-9_-]", "", s)
        return s

    # exact match
    for row in rows:
        if row["hoa_name"].lower() == slug.lower():
            return {
                "hoa_id": int(row["hoa_id"]),
                "hoa_name": str(row["hoa_name"]),
                "city": str(row["city"]) if row["city"] else None,
                "state": str(row["state"]) if row["state"] else None,
            }

    # slug match (underscores and dashes treated as equivalent)
    normalized = slug.replace("-", "_")
    for row in rows:
        row_slug = slugify(row["hoa_name"])
        if row_slug == normalized or row_slug.replace("_", "-") == slug:
            return {
                "hoa_id": int(row["hoa_id"]),
                "hoa_name": str(row["hoa_name"]),
                "city": str(row["city"]) if row["city"] else None,
                "state": str(row["state"]) if row["state"] else None,
            }

    return None


def resolve_hoa_by_hierarchical_slug(
    conn: sqlite3.Connection,
    state: str,
    city_slug: str,
    name_slug: str,
) -> dict | None:
    """Resolve an HOA by its state/city/name slug triple.

    Filters by state in SQL so only a small subset is checked in Python.
    Returns ``{hoa_id, hoa_name, city, state, doc_count}`` or *None*.
    """
    cur = conn.execute(
        """
        SELECT h.id   AS hoa_id,
               h.name AS hoa_name,
               l.city,
               l.state,
               COALESCE(ds.doc_count, 0) AS doc_count
        FROM hoas h
        LEFT JOIN hoa_locations l ON l.hoa_id = h.id
        LEFT JOIN (
            SELECT hoa_id, COUNT(*) AS doc_count FROM documents GROUP BY hoa_id
        ) ds ON ds.hoa_id = h.id
        WHERE LOWER(l.state) = ?
        """,
        (state.lower(),),
    )
    for row in cur:
        if (
            slugify_city(row["city"] or "") == city_slug
            and slugify_name(row["hoa_name"]) == name_slug
        ):
            return {
                "hoa_id": int(row["hoa_id"]),
                "hoa_name": str(row["hoa_name"]),
                "city": str(row["city"]) if row["city"] else None,
                "state": str(row["state"]) if row["state"] else None,
                "doc_count": int(row["doc_count"]),
            }
    return None


def list_hoas_for_sitemap(conn: sqlite3.Connection) -> list[dict]:
    """Return lightweight data for every HOA (sitemap + index pages)."""
    cur = conn.execute(
        """
        SELECT h.id   AS hoa_id,
               h.name AS hoa_name,
               l.city,
               l.state,
               COALESCE(ds.doc_count, 0) AS doc_count
        FROM hoas h
        LEFT JOIN hoa_locations l ON l.hoa_id = h.id
        LEFT JOIN (
            SELECT hoa_id, COUNT(*) AS doc_count FROM documents GROUP BY hoa_id
        ) ds ON ds.hoa_id = h.id
        ORDER BY l.state, l.city, h.name
        """
    )
    return [dict(row) for row in cur.fetchall()]


def list_cities_in_state(
    conn: sqlite3.Connection, state: str
) -> list[dict]:
    """Return ``[{city, hoa_count}, ...]`` for a given state."""
    cur = conn.execute(
        """
        SELECT l.city, COUNT(*) AS hoa_count
        FROM hoas h
        JOIN hoa_locations l ON l.hoa_id = h.id
        WHERE LOWER(l.state) = ? AND l.city IS NOT NULL AND l.city != ''
        GROUP BY l.city
        ORDER BY l.city COLLATE NOCASE
        """,
        (state.lower(),),
    )
    return [dict(row) for row in cur.fetchall()]


def list_hoas_in_city(
    conn: sqlite3.Connection, state: str, city_slug: str
) -> list[dict]:
    """Return HOAs in a city (matched by slug).

    Returns ``[{hoa_id, hoa_name, city, state, doc_count}, ...]``.
    """
    cur = conn.execute(
        """
        SELECT h.id   AS hoa_id,
               h.name AS hoa_name,
               l.city,
               l.state,
               COALESCE(ds.doc_count, 0) AS doc_count
        FROM hoas h
        JOIN hoa_locations l ON l.hoa_id = h.id
        LEFT JOIN (
            SELECT hoa_id, COUNT(*) AS doc_count FROM documents GROUP BY hoa_id
        ) ds ON ds.hoa_id = h.id
        WHERE LOWER(l.state) = ?
        ORDER BY h.name COLLATE NOCASE
        """,
        (state.lower(),),
    )
    return [
        dict(row) for row in cur.fetchall()
        if slugify_city(row["city"] or "") == city_slug
    ]


def get_hoa_location(conn: sqlite3.Connection, hoa_name: str) -> dict | None:
    cur = conn.execute(
        """
        SELECT
            h.name AS hoa,
            l.metadata_type,
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
            "metadata_type": None,
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
        "metadata_type": str(row["metadata_type"]) if row["metadata_type"] is not None else None,
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
    metadata_type: str | None = None,
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
                (hoa_id, metadata_type, display_name, website_url, street, city, state, postal_code, country, latitude, longitude, boundary_geojson, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hoa_id,
                metadata_type,
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
            metadata_type = COALESCE(?, metadata_type),
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
            metadata_type,
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
            l.metadata_type,
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
        LEFT JOIN documents d ON d.hoa_id = h.id
        LEFT JOIN hoa_locations l ON l.hoa_id = h.id
        GROUP BY h.id
        ORDER BY h.name COLLATE NOCASE
        """
    )
    rows = cur.fetchall()
    return [
        {
            "hoa": str(row["hoa"]),
            "metadata_type": str(row["metadata_type"]) if row["metadata_type"] is not None else None,
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


def vector_search(
    conn: sqlite3.Connection,
    hoa_name: str,
    query_vector: list[float],
    limit: int = 5,
) -> list[dict]:
    """Brute-force cosine similarity search over chunk embeddings for one HOA.

    Returns a list of dicts with keys: score, payload (hoa, document, chunk_index,
    start_page, end_page, text).
    """
    import numpy as np

    cur = conn.execute(
        """
        SELECT c.embedding, c.text, c.chunk_index, c.start_page, c.end_page,
               d.relative_path
        FROM chunks c
        JOIN documents d ON c.document_id = d.id
        JOIN hoas h ON d.hoa_id = h.id
        WHERE h.name = ? AND c.embedding IS NOT NULL
        """,
        (hoa_name,),
    )
    rows = cur.fetchall()
    if not rows:
        return []

    # Parse embeddings from BLOBs
    embeddings = []
    metadata = []
    for row in rows:
        vec = np.frombuffer(row["embedding"], dtype=np.float32)
        embeddings.append(vec)
        metadata.append({
            "hoa": hoa_name,
            "document": row["relative_path"],
            "chunk_index": row["chunk_index"],
            "start_page": row["start_page"],
            "end_page": row["end_page"],
            "text": row["text"],
        })

    # Cosine similarity
    query = np.array(query_vector, dtype=np.float32)
    matrix = np.stack(embeddings)
    # Normalize
    query_norm = query / (np.linalg.norm(query) + 1e-10)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-10
    matrix_norm = matrix / norms
    scores = matrix_norm @ query_norm

    # Top-k
    top_indices = np.argsort(scores)[::-1][:limit]
    results = []
    for idx in top_indices:
        results.append({
            "score": float(scores[idx]),
            "payload": metadata[idx],
        })
    return results


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


def _normalize_scope_token(value: str) -> str:
    return value.strip().upper()


def upsert_legal_source(
    conn: sqlite3.Connection,
    *,
    jurisdiction: str,
    community_type: str,
    entity_form: str,
    governing_law_bucket: str,
    source_type: str,
    citation: str,
    citation_url: str,
    publisher: str | None = None,
    effective_date: str | None = None,
    last_verified_date: str | None = None,
    checksum_sha256: str | None = None,
    snapshot_path: str | None = None,
    parser_version: str | None = None,
    notes: str | None = None,
) -> int:
    jurisdiction_norm = _normalize_scope_token(jurisdiction)
    community_norm = community_type.strip().lower()
    entity_norm = entity_form.strip().lower()
    bucket_norm = governing_law_bucket.strip().lower()
    source_type_norm = source_type.strip().lower()
    row = conn.execute(
        """
        SELECT id
        FROM legal_sources
        WHERE jurisdiction = ?
          AND community_type = ?
          AND entity_form = ?
          AND governing_law_bucket = ?
          AND citation = ?
          AND citation_url = ?
        """,
        (
            jurisdiction_norm,
            community_norm,
            entity_norm,
            bucket_norm,
            citation.strip(),
            citation_url.strip(),
        ),
    ).fetchone()
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO legal_sources (
                jurisdiction,
                community_type,
                entity_form,
                governing_law_bucket,
                source_type,
                publisher,
                citation,
                citation_url,
                effective_date,
                last_verified_date,
                checksum_sha256,
                snapshot_path,
                parser_version,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                jurisdiction_norm,
                community_norm,
                entity_norm,
                bucket_norm,
                source_type_norm,
                publisher,
                citation.strip(),
                citation_url.strip(),
                effective_date,
                last_verified_date,
                checksum_sha256,
                snapshot_path,
                parser_version,
                notes,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    source_id = int(row["id"])
    conn.execute(
        """
        UPDATE legal_sources
        SET
            source_type = ?,
            publisher = COALESCE(?, publisher),
            effective_date = COALESCE(?, effective_date),
            last_verified_date = COALESCE(?, last_verified_date),
            checksum_sha256 = COALESCE(?, checksum_sha256),
            snapshot_path = COALESCE(?, snapshot_path),
            parser_version = COALESCE(?, parser_version),
            notes = COALESCE(?, notes),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            source_type_norm,
            publisher,
            effective_date,
            last_verified_date,
            checksum_sha256,
            snapshot_path,
            parser_version,
            notes,
            source_id,
        ),
    )
    conn.commit()
    return source_id


def replace_legal_sections(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    sections: Sequence[dict[str, Any]],
) -> list[int]:
    conn.execute("DELETE FROM legal_sections WHERE source_id = ?", (source_id,))
    section_ids: list[int] = []
    for idx, section in enumerate(sections):
        section_key = str(section.get("section_key") or f"section_{idx + 1}")
        heading = section.get("heading")
        text = str(section.get("text") or "").strip()
        if not text:
            continue
        checksum_sha256 = section.get("checksum_sha256")
        cur = conn.execute(
            """
            INSERT INTO legal_sections (
                source_id, section_key, heading, text, ordinal, checksum_sha256
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                section_key,
                str(heading).strip() if heading else None,
                text,
                idx,
                str(checksum_sha256).strip() if checksum_sha256 else None,
            ),
        )
        section_ids.append(int(cur.lastrowid))
    conn.commit()
    return section_ids


def upsert_legal_rule(
    conn: sqlite3.Connection,
    *,
    jurisdiction: str,
    community_type: str,
    entity_form: str,
    topic_family: str,
    rule_type: str,
    value_text: str,
    citation: str,
    citation_url: str | None = None,
    applies_to: str | None = None,
    value_numeric: float | None = None,
    value_unit: str | None = None,
    conditions: str | None = None,
    exceptions: str | None = None,
    source_id: int | None = None,
    section_id: int | None = None,
    effective_date: str | None = None,
    last_verified_date: str | None = None,
    confidence: str = "medium",
    needs_human_review: int = 0,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO legal_rules (
            jurisdiction,
            community_type,
            entity_form,
            topic_family,
            rule_type,
            applies_to,
            value_text,
            value_numeric,
            value_unit,
            conditions,
            exceptions,
            source_id,
            section_id,
            citation,
            citation_url,
            effective_date,
            last_verified_date,
            confidence,
            needs_human_review
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _normalize_scope_token(jurisdiction),
            community_type.strip().lower(),
            entity_form.strip().lower(),
            topic_family.strip().lower(),
            rule_type.strip().lower(),
            applies_to.strip().lower() if applies_to else None,
            value_text.strip(),
            value_numeric,
            value_unit.strip().lower() if value_unit else None,
            conditions.strip() if conditions else None,
            exceptions.strip() if exceptions else None,
            source_id,
            section_id,
            citation.strip(),
            citation_url.strip() if citation_url else None,
            effective_date,
            last_verified_date,
            confidence.strip().lower(),
            1 if needs_human_review else 0,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def replace_legal_rules_for_scope(
    conn: sqlite3.Connection,
    *,
    jurisdiction: str,
    community_type: str,
    entity_form: str,
    topic_family: str,
    rules: Sequence[dict[str, Any]],
) -> int:
    jurisdiction_norm = _normalize_scope_token(jurisdiction)
    community_norm = community_type.strip().lower()
    entity_norm = entity_form.strip().lower()
    topic_norm = topic_family.strip().lower()
    conn.execute(
        """
        DELETE FROM legal_rules
        WHERE jurisdiction = ?
          AND community_type = ?
          AND entity_form = ?
          AND topic_family = ?
        """,
        (jurisdiction_norm, community_norm, entity_norm, topic_norm),
    )
    inserted = 0
    for rule in rules:
        value_text = str(rule.get("value_text") or "").strip()
        citation = str(rule.get("citation") or "").strip()
        if not value_text or not citation:
            continue
        upsert_legal_rule(
            conn,
            jurisdiction=jurisdiction_norm,
            community_type=community_norm,
            entity_form=entity_norm,
            topic_family=topic_norm,
            rule_type=str(rule.get("rule_type") or "unspecified"),
            value_text=value_text,
            citation=citation,
            citation_url=rule.get("citation_url"),
            applies_to=rule.get("applies_to"),
            value_numeric=rule.get("value_numeric"),
            value_unit=rule.get("value_unit"),
            conditions=rule.get("conditions"),
            exceptions=rule.get("exceptions"),
            source_id=rule.get("source_id"),
            section_id=rule.get("section_id"),
            effective_date=rule.get("effective_date"),
            last_verified_date=rule.get("last_verified_date"),
            confidence=str(rule.get("confidence") or "medium"),
            needs_human_review=int(rule.get("needs_human_review") or 0),
        )
        inserted += 1
    return inserted


def list_legal_sources(
    conn: sqlite3.Connection,
    *,
    jurisdiction: str | None = None,
    community_type: str | None = None,
    entity_form: str | None = None,
) -> list[dict]:
    query = "SELECT * FROM legal_sources WHERE 1=1"
    params: list[Any] = []
    if jurisdiction:
        query += " AND jurisdiction = ?"
        params.append(_normalize_scope_token(jurisdiction))
    if community_type:
        query += " AND community_type = ?"
        params.append(community_type.strip().lower())
    if entity_form:
        query += " AND entity_form = ?"
        params.append(entity_form.strip().lower())
    query += " ORDER BY jurisdiction, community_type, entity_form, governing_law_bucket, citation"
    rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": int(row["id"]),
            "jurisdiction": str(row["jurisdiction"]),
            "community_type": str(row["community_type"]),
            "entity_form": str(row["entity_form"]),
            "governing_law_bucket": str(row["governing_law_bucket"]),
            "source_type": str(row["source_type"]),
            "publisher": str(row["publisher"]) if row["publisher"] is not None else None,
            "citation": str(row["citation"]),
            "citation_url": str(row["citation_url"]),
            "effective_date": str(row["effective_date"]) if row["effective_date"] is not None else None,
            "last_verified_date": str(row["last_verified_date"]) if row["last_verified_date"] is not None else None,
            "checksum_sha256": str(row["checksum_sha256"]) if row["checksum_sha256"] is not None else None,
            "snapshot_path": str(row["snapshot_path"]) if row["snapshot_path"] is not None else None,
            "parser_version": str(row["parser_version"]) if row["parser_version"] is not None else None,
            "notes": str(row["notes"]) if row["notes"] is not None else None,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


def list_legal_rules_for_scope(
    conn: sqlite3.Connection,
    *,
    jurisdiction: str,
    community_type: str,
    entity_form: str = "unknown",
    topic_family: str | None = None,
) -> list[dict]:
    jurisdiction_norm = _normalize_scope_token(jurisdiction)
    community_norm = community_type.strip().lower()
    entity_norm = entity_form.strip().lower()
    rows = conn.execute(
        """
        SELECT
            r.*,
            s.source_type AS source_type
        FROM legal_rules r
        LEFT JOIN legal_sources s ON s.id = r.source_id
        WHERE r.jurisdiction = ?
          AND r.community_type = ?
          AND (r.entity_form = ? OR r.entity_form = 'unknown')
          AND (? IS NULL OR r.topic_family = ?)
        ORDER BY r.topic_family, r.rule_type, r.id
        """,
        (jurisdiction_norm, community_norm, entity_norm, topic_family, topic_family),
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "jurisdiction": str(row["jurisdiction"]),
            "community_type": str(row["community_type"]),
            "entity_form": str(row["entity_form"]),
            "topic_family": str(row["topic_family"]),
            "rule_type": str(row["rule_type"]),
            "applies_to": str(row["applies_to"]) if row["applies_to"] is not None else None,
            "value_text": str(row["value_text"]),
            "value_numeric": float(row["value_numeric"]) if row["value_numeric"] is not None else None,
            "value_unit": str(row["value_unit"]) if row["value_unit"] is not None else None,
            "conditions": str(row["conditions"]) if row["conditions"] is not None else None,
            "exceptions": str(row["exceptions"]) if row["exceptions"] is not None else None,
            "source_id": int(row["source_id"]) if row["source_id"] is not None else None,
            "section_id": int(row["section_id"]) if row["section_id"] is not None else None,
            "citation": str(row["citation"]),
            "citation_url": str(row["citation_url"]) if row["citation_url"] is not None else None,
            "source_type": str(row["source_type"]) if row["source_type"] is not None else "unknown",
            "effective_date": str(row["effective_date"]) if row["effective_date"] is not None else None,
            "last_verified_date": str(row["last_verified_date"]) if row["last_verified_date"] is not None else None,
            "confidence": str(row["confidence"]),
            "needs_human_review": int(row["needs_human_review"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


def upsert_jurisdiction_profile(
    conn: sqlite3.Connection,
    *,
    jurisdiction: str,
    community_type: str,
    entity_form: str,
    governing_law_stack: Sequence[dict[str, Any]],
    records_access_summary: str | None,
    records_sharing_limits_summary: str | None,
    proxy_voting_summary: str | None,
    conflict_resolution_notes: str | None,
    known_gaps: Sequence[str],
    confidence: str,
    last_verified_date: str | None,
    source_rule_count: int,
) -> int:
    jurisdiction_norm = _normalize_scope_token(jurisdiction)
    community_norm = community_type.strip().lower()
    entity_norm = entity_form.strip().lower()
    row = conn.execute(
        """
        SELECT id
        FROM jurisdiction_profiles
        WHERE jurisdiction = ? AND community_type = ? AND entity_form = ?
        """,
        (jurisdiction_norm, community_norm, entity_norm),
    ).fetchone()
    payload_stack = json.dumps(list(governing_law_stack), separators=(",", ":"))
    payload_gaps = json.dumps(list(known_gaps), separators=(",", ":"))
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO jurisdiction_profiles (
                jurisdiction,
                community_type,
                entity_form,
                governing_law_stack,
                records_access_summary,
                records_sharing_limits_summary,
                proxy_voting_summary,
                conflict_resolution_notes,
                known_gaps,
                confidence,
                last_verified_date,
                source_rule_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                jurisdiction_norm,
                community_norm,
                entity_norm,
                payload_stack,
                records_access_summary,
                records_sharing_limits_summary,
                proxy_voting_summary,
                conflict_resolution_notes,
                payload_gaps,
                confidence.strip().lower(),
                last_verified_date,
                int(source_rule_count),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    profile_id = int(row["id"])
    conn.execute(
        """
        UPDATE jurisdiction_profiles
        SET
            governing_law_stack = ?,
            records_access_summary = ?,
            records_sharing_limits_summary = ?,
            proxy_voting_summary = ?,
            conflict_resolution_notes = ?,
            known_gaps = ?,
            confidence = ?,
            last_verified_date = ?,
            source_rule_count = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            payload_stack,
            records_access_summary,
            records_sharing_limits_summary,
            proxy_voting_summary,
            conflict_resolution_notes,
            payload_gaps,
            confidence.strip().lower(),
            last_verified_date,
            int(source_rule_count),
            profile_id,
        ),
    )
    conn.commit()
    return profile_id


def list_jurisdiction_profiles(
    conn: sqlite3.Connection,
    *,
    jurisdiction: str | None = None,
    community_type: str | None = None,
    entity_form: str | None = None,
) -> list[dict]:
    query = "SELECT * FROM jurisdiction_profiles WHERE 1=1"
    params: list[Any] = []
    if jurisdiction:
        query += " AND jurisdiction = ?"
        params.append(_normalize_scope_token(jurisdiction))
    if community_type:
        query += " AND community_type = ?"
        params.append(community_type.strip().lower())
    if entity_form:
        query += " AND entity_form = ?"
        params.append(entity_form.strip().lower())
    query += " ORDER BY jurisdiction, community_type, entity_form"
    rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": int(row["id"]),
            "jurisdiction": str(row["jurisdiction"]),
            "community_type": str(row["community_type"]),
            "entity_form": str(row["entity_form"]),
            "governing_law_stack": _load_json_list(row["governing_law_stack"]),
            "records_access_summary": str(row["records_access_summary"]) if row["records_access_summary"] is not None else None,
            "records_sharing_limits_summary": str(row["records_sharing_limits_summary"]) if row["records_sharing_limits_summary"] is not None else None,
            "proxy_voting_summary": str(row["proxy_voting_summary"]) if row["proxy_voting_summary"] is not None else None,
            "conflict_resolution_notes": str(row["conflict_resolution_notes"]) if row["conflict_resolution_notes"] is not None else None,
            "known_gaps": _load_json_list(row["known_gaps"]),
            "confidence": str(row["confidence"]),
            "last_verified_date": str(row["last_verified_date"]) if row["last_verified_date"] is not None else None,
            "source_rule_count": int(row["source_rule_count"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


def get_jurisdiction_profile(
    conn: sqlite3.Connection,
    *,
    jurisdiction: str,
    community_type: str,
    entity_form: str,
) -> dict | None:
    jurisdiction_norm = _normalize_scope_token(jurisdiction)
    community_norm = community_type.strip().lower()
    entity_norm = entity_form.strip().lower()
    rows = conn.execute(
        """
        SELECT *
        FROM jurisdiction_profiles
        WHERE jurisdiction = ?
          AND community_type = ?
          AND entity_form IN (?, 'unknown')
        ORDER BY CASE WHEN entity_form = ? THEN 0 ELSE 1 END, updated_at DESC
        LIMIT 1
        """,
        (jurisdiction_norm, community_norm, entity_norm, entity_norm),
    ).fetchall()
    if not rows:
        return None
    row = rows[0]
    return {
        "id": int(row["id"]),
        "jurisdiction": str(row["jurisdiction"]),
        "community_type": str(row["community_type"]),
        "entity_form": str(row["entity_form"]),
        "governing_law_stack": _load_json_list(row["governing_law_stack"]),
        "records_access_summary": str(row["records_access_summary"]) if row["records_access_summary"] is not None else None,
        "records_sharing_limits_summary": str(row["records_sharing_limits_summary"]) if row["records_sharing_limits_summary"] is not None else None,
        "proxy_voting_summary": str(row["proxy_voting_summary"]) if row["proxy_voting_summary"] is not None else None,
        "conflict_resolution_notes": str(row["conflict_resolution_notes"]) if row["conflict_resolution_notes"] is not None else None,
        "known_gaps": _load_json_list(row["known_gaps"]),
        "confidence": str(row["confidence"]),
        "last_verified_date": str(row["last_verified_date"]) if row["last_verified_date"] is not None else None,
        "source_rule_count": int(row["source_rule_count"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def list_law_jurisdictions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            jurisdiction,
            COUNT(DISTINCT community_type) AS community_types,
            COUNT(*) AS profile_count,
            MAX(last_verified_date) AS last_verified_date,
            SUM(source_rule_count) AS rule_count
        FROM jurisdiction_profiles
        GROUP BY jurisdiction
        ORDER BY jurisdiction
        """
    ).fetchall()
    return [
        {
            "jurisdiction": str(row["jurisdiction"]),
            "community_types": int(row["community_types"]),
            "profile_count": int(row["profile_count"]),
            "last_verified_date": str(row["last_verified_date"]) if row["last_verified_date"] is not None else None,
            "rule_count": int(row["rule_count"]) if row["rule_count"] is not None else 0,
        }
        for row in rows
    ]


def create_legal_ingest_run(
    conn: sqlite3.Connection,
    *,
    run_phase: str,
    status: str,
    details: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO legal_ingest_runs (run_phase, status, details_json)
        VALUES (?, ?, ?)
        """,
        (
            run_phase.strip(),
            status.strip().lower(),
            json.dumps(details or {}, separators=(",", ":")),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def finalize_legal_ingest_run(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    status: str,
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        UPDATE legal_ingest_runs
        SET
            status = ?,
            details_json = COALESCE(?, details_json),
            finished_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            status.strip().lower(),
            json.dumps(details, separators=(",", ":")) if details is not None else None,
            int(run_id),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(
    conn: sqlite3.Connection,
    *,
    email: str,
    password_hash: str | None = None,
    display_name: str | None = None,
    google_id: str | None = None,
    verified_at: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO users (email, password_hash, display_name, verified_at, google_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (email.strip().lower(), password_hash, (display_name or "").strip() or None, verified_at, google_id),
    )
    conn.commit()
    return int(cur.lastrowid)


def create_verification_token(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    token: str,
    expires_at: str,
) -> None:
    conn.execute("DELETE FROM email_verification_tokens WHERE user_id = ?", (user_id,))
    conn.execute(
        "INSERT INTO email_verification_tokens (user_id, token, expires_at) VALUES (?, ?, ?)",
        (user_id, token, expires_at),
    )
    conn.commit()


def get_verification_token(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM email_verification_tokens WHERE token = ?", (token,)
    ).fetchone()
    return dict(row) if row else None


def mark_user_verified(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute(
        "UPDATE users SET verified_at = CURRENT_TIMESTAMP WHERE id = ?", (user_id,)
    )
    conn.execute("DELETE FROM email_verification_tokens WHERE user_id = ?", (user_id,))
    conn.commit()


def create_password_reset_token(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    token: str,
    expires_at: str,
) -> None:
    conn.execute("DELETE FROM password_reset_tokens WHERE user_id = ?", (user_id,))
    conn.execute(
        "INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (?, ?, ?)",
        (user_id, token, expires_at),
    )
    conn.commit()


def get_password_reset_token(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM password_reset_tokens WHERE token = ? AND used_at IS NULL",
        (token,),
    ).fetchone()
    return dict(row) if row else None


def consume_password_reset_token(conn: sqlite3.Connection, token: str, new_password_hash: str) -> bool:
    """Mark token used and update the user's password. Returns False if token not found."""
    row = conn.execute(
        "SELECT * FROM password_reset_tokens WHERE token = ? AND used_at IS NULL",
        (token,),
    ).fetchone()
    if not row:
        return False
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (new_password_hash, row["user_id"]),
    )
    conn.execute(
        "UPDATE password_reset_tokens SET used_at = CURRENT_TIMESTAMP WHERE id = ?",
        (row["id"],),
    )
    conn.commit()
    return True


def get_user_by_email(conn: sqlite3.Connection, email: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM users WHERE email = ?",
        (email.strip().lower(),),
    ).fetchone()
    if not row:
        return None
    return dict(row)


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
    if not row:
        return None
    return dict(row)


def get_user_by_google_id(conn: sqlite3.Connection, google_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE google_id = ?", (google_id,)).fetchone()
    if not row:
        return None
    return dict(row)


def link_google_id(conn: sqlite3.Connection, user_id: int, google_id: str) -> None:
    conn.execute("UPDATE users SET google_id = ? WHERE id = ?", (google_id, user_id))
    conn.commit()


def update_user(conn: sqlite3.Connection, user_id: int, *, display_name: str | None = None, email: str | None = None, password_hash: str | None = None) -> dict | None:
    """Update mutable user fields. Only non-None kwargs are applied."""
    sets: list[str] = []
    params: list = []
    if display_name is not None:
        sets.append("display_name = ?")
        params.append(display_name)
    if email is not None:
        sets.append("email = ?")
        params.append(email.strip().lower())
    if password_hash is not None:
        sets.append("password_hash = ?")
        params.append(password_hash)
    if not sets:
        return get_user_by_id(conn, user_id)
    params.append(int(user_id))
    conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    return get_user_by_id(conn, user_id)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    token_jti: str,
    expires_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO sessions (user_id, token_jti, expires_at)
        VALUES (?, ?, ?)
        """,
        (int(user_id), token_jti, expires_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_session_by_jti(conn: sqlite3.Connection, jti: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM sessions WHERE token_jti = ?",
        (jti,),
    ).fetchone()
    if not row:
        return None
    return dict(row)


def delete_session_by_jti(conn: sqlite3.Connection, jti: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_jti = ?", (jti,))
    conn.commit()


# ---------------------------------------------------------------------------
# Membership Claims
# ---------------------------------------------------------------------------

def create_membership_claim(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    hoa_id: int,
    unit_number: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO membership_claims (user_id, hoa_id, unit_number)
        VALUES (?, ?, ?)
        """,
        (int(user_id), int(hoa_id), (unit_number or "").strip() or None),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_membership_claim(
    conn: sqlite3.Connection, user_id: int, hoa_id: int
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM membership_claims WHERE user_id = ? AND hoa_id = ?",
        (int(user_id), int(hoa_id)),
    ).fetchone()
    if not row:
        return None
    return dict(row)


def list_membership_claims_for_user(
    conn: sqlite3.Connection, user_id: int
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT mc.*, h.name AS hoa_name
        FROM membership_claims mc
        JOIN hoas h ON h.id = mc.hoa_id
        WHERE mc.user_id = ?
        ORDER BY mc.created_at DESC
        """,
        (int(user_id),),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Delegates
# ---------------------------------------------------------------------------

def create_delegate(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    hoa_id: int,
    bio: str | None = None,
    contact_email: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO delegates (user_id, hoa_id, bio, contact_email)
        VALUES (?, ?, ?, ?)
        """,
        (int(user_id), int(hoa_id), (bio or "").strip() or None, (contact_email or "").strip() or None),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_delegate(conn: sqlite3.Connection, delegate_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT d.*, u.display_name, u.email AS user_email, h.name AS hoa_name
        FROM delegates d
        JOIN users u ON u.id = d.user_id
        JOIN hoas h ON h.id = d.hoa_id
        WHERE d.id = ?
        """,
        (int(delegate_id),),
    ).fetchone()
    if not row:
        return None
    return dict(row)


def get_delegate_by_user_hoa(
    conn: sqlite3.Connection, user_id: int, hoa_id: int
) -> dict | None:
    row = conn.execute(
        """
        SELECT d.*, u.display_name, u.email AS user_email, h.name AS hoa_name
        FROM delegates d
        JOIN users u ON u.id = d.user_id
        JOIN hoas h ON h.id = d.hoa_id
        WHERE d.user_id = ? AND d.hoa_id = ?
        """,
        (int(user_id), int(hoa_id)),
    ).fetchone()
    if not row:
        return None
    return dict(row)


def list_delegates_for_hoa(conn: sqlite3.Connection, hoa_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT d.*, u.display_name, u.email AS user_email
        FROM delegates d
        JOIN users u ON u.id = d.user_id
        WHERE d.hoa_id = ?
        ORDER BY d.created_at DESC
        """,
        (int(hoa_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def update_delegate(
    conn: sqlite3.Connection,
    delegate_id: int,
    *,
    bio: str | None = None,
    contact_email: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE delegates SET bio = ?, contact_email = ? WHERE id = ?
        """,
        ((bio or "").strip() or None, (contact_email or "").strip() or None, int(delegate_id)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Proxy Assignments
# ---------------------------------------------------------------------------

def create_proxy_assignment(
    conn: sqlite3.Connection,
    *,
    grantor_user_id: int,
    delegate_user_id: int,
    hoa_id: int,
    jurisdiction: str,
    community_type: str,
    direction: str = "directed",
    voting_instructions: str | None = None,
    for_meeting_date: str | None = None,
    expires_at: str | None = None,
    form_html: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO proxy_assignments
            (grantor_user_id, delegate_user_id, hoa_id, jurisdiction, community_type,
             direction, voting_instructions, for_meeting_date, expires_at, form_html)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(grantor_user_id), int(delegate_user_id), int(hoa_id),
            jurisdiction.strip(), community_type.strip(),
            direction, voting_instructions, for_meeting_date, expires_at, form_html,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_proxy_assignment(conn: sqlite3.Connection, proxy_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT pa.*,
               gu.email AS grantor_email, gu.display_name AS grantor_name,
               du.email AS delegate_email, du.display_name AS delegate_name,
               h.name AS hoa_name, h.board_email AS hoa_board_email
        FROM proxy_assignments pa
        JOIN users gu ON gu.id = pa.grantor_user_id
        JOIN users du ON du.id = pa.delegate_user_id
        JOIN hoas h ON h.id = pa.hoa_id
        WHERE pa.id = ?
        """,
        (int(proxy_id),),
    ).fetchone()
    if not row:
        return None
    return dict(row)


def update_proxy_status(
    conn: sqlite3.Connection,
    proxy_id: int,
    status: str,
    **extra_fields: Any,
) -> None:
    sets = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
    vals: list[Any] = [status]
    for col, val in extra_fields.items():
        sets.append(f"{col} = ?")
        vals.append(val)
    vals.append(int(proxy_id))
    conn.execute(
        f"UPDATE proxy_assignments SET {', '.join(sets)} WHERE id = ?",
        vals,
    )
    conn.commit()


def list_proxies_for_grantor(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT pa.*, du.display_name AS delegate_name, h.name AS hoa_name
        FROM proxy_assignments pa
        JOIN users du ON du.id = pa.delegate_user_id
        JOIN hoas h ON h.id = pa.hoa_id
        WHERE pa.grantor_user_id = ?
        ORDER BY pa.created_at DESC
        """,
        (int(user_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def get_active_proxy_for_grantor_hoa(
    conn: sqlite3.Connection,
    user_id: int,
    hoa_id: int,
) -> dict | None:
    row = conn.execute(
        """
        SELECT pa.*, du.display_name AS delegate_name, h.name AS hoa_name
        FROM proxy_assignments pa
        JOIN users du ON du.id = pa.delegate_user_id
        JOIN hoas h ON h.id = pa.hoa_id
        WHERE pa.grantor_user_id = ?
          AND pa.hoa_id = ?
          AND pa.status NOT IN ('revoked', 'expired', 'purged')
        ORDER BY pa.created_at DESC
        LIMIT 1
        """,
        (int(user_id), int(hoa_id)),
    ).fetchone()
    return dict(row) if row else None


def list_proxies_for_delegate(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT pa.*, gu.display_name AS grantor_name, h.name AS hoa_name
        FROM proxy_assignments pa
        JOIN users gu ON gu.id = pa.grantor_user_id
        JOIN hoas h ON h.id = pa.hoa_id
        WHERE pa.delegate_user_id = ?
        ORDER BY pa.created_at DESC
        """,
        (int(user_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def count_proxies_for_hoa(conn: sqlite3.Connection, hoa_id: int) -> dict:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN status = 'signed' THEN 1 ELSE 0 END), 0) AS signed,
            COALESCE(SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END), 0) AS delivered
        FROM proxy_assignments
        WHERE hoa_id = ? AND status NOT IN ('revoked', 'expired')
        """,
        (int(hoa_id),),
    ).fetchone()
    return dict(row) if row else {"total": 0, "signed": 0, "delivered": 0}


# ---------------------------------------------------------------------------
# Proxy Audit
# ---------------------------------------------------------------------------

def create_proxy_audit(
    conn: sqlite3.Connection,
    *,
    proxy_id: int,
    action: str,
    actor_user_id: int | None = None,
    details: dict | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO proxy_audit (proxy_id, action, actor_user_id, details)
        VALUES (?, ?, ?, ?)
        """,
        (int(proxy_id), action, actor_user_id, json.dumps(details or {})),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_proxy_audit(conn: sqlite3.Connection, proxy_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM proxy_audit WHERE proxy_id = ? ORDER BY created_at",
        (int(proxy_id),),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Participation Records
# ---------------------------------------------------------------------------

def create_participation_record(
    conn: sqlite3.Connection,
    *,
    hoa_id: int,
    meeting_date: str,
    meeting_type: str | None = None,
    total_units: int | None = None,
    votes_cast: int | None = None,
    quorum_required: int | None = None,
    quorum_met: bool | None = None,
    source_document_id: int | None = None,
    entered_by_user_id: int | None = None,
    notes: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO participation_records
            (hoa_id, meeting_date, meeting_type, total_units, votes_cast,
             quorum_required, quorum_met, source_document_id, entered_by_user_id, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(hoa_id), meeting_date, meeting_type, total_units, votes_cast,
            quorum_required, quorum_met, source_document_id, entered_by_user_id,
            (notes or "").strip() or None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_participation_records(conn: sqlite3.Connection, hoa_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM participation_records
        WHERE hoa_id = ?
        ORDER BY meeting_date DESC
        """,
        (int(hoa_id),),
    ).fetchall()
    return [dict(r) for r in rows]


_SEEDS_DIR = Path(__file__).parent / "seeds"


def seed_legal_data(conn: sqlite3.Connection) -> int:
    """Seed legal_rules and jurisdiction_profiles from bundled gzip seed files
    if those tables are currently empty. Returns the number of rows inserted."""
    if not _SEEDS_DIR.exists():
        return 0
    inserted = 0

    # Seed jurisdiction_profiles
    jp_count = conn.execute("SELECT COUNT(*) FROM jurisdiction_profiles").fetchone()[0]
    jp_seed = _SEEDS_DIR / "jurisdiction_profiles.jsonl.gz"
    if jp_count == 0 and jp_seed.exists():
        with gzip.open(jp_seed, "rt", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        for row in rows:
            row.pop("id", None)
            cols = list(row.keys())
            placeholders = ", ".join("?" for _ in cols)
            conn.execute(
                f"INSERT OR IGNORE INTO jurisdiction_profiles ({', '.join(cols)}) VALUES ({placeholders})",
                [row[c] for c in cols],
            )
        conn.commit()
        inserted += len(rows)

    # Seed legal_rules (proxy_voting only)
    lr_count = conn.execute(
        "SELECT COUNT(*) FROM legal_rules WHERE topic_family='proxy_voting'"
    ).fetchone()[0]
    lr_seed = _SEEDS_DIR / "legal_rules_proxy_voting.jsonl.gz"
    if lr_count == 0 and lr_seed.exists():
        with gzip.open(lr_seed, "rt", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        for row in rows:
            row.pop("id", None)
            # source_id / section_id reference tables not present on fresh DBs
            row["source_id"] = None
            row["section_id"] = None
            cols = list(row.keys())
            placeholders = ", ".join("?" for _ in cols)
            conn.execute(
                f"INSERT OR IGNORE INTO legal_rules ({', '.join(cols)}) VALUES ({placeholders})",
                [row[c] for c in cols],
            )
        conn.commit()
        inserted += len(rows)

    return inserted


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------

_SHARE_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 chars, no 0/O/I/1


def _generate_share_code(conn: sqlite3.Connection) -> str:
    import random
    for _ in range(20):
        code = "".join(random.choices(_SHARE_CODE_ALPHABET, k=4))
        row = conn.execute("SELECT id FROM proposals WHERE share_code = ?", (code,)).fetchone()
        if row is None:
            return code
    raise RuntimeError("Could not generate unique share code after 20 attempts")


def create_proposal(
    conn: sqlite3.Connection,
    *,
    hoa_id: int,
    creator_user_id: int,
    title: str,
    description: str,
    category: str = "Other",
    lat: float | None = None,
    lng: float | None = None,
    location_description: str | None = None,
) -> int:
    share_code = _generate_share_code(conn)
    cur = conn.execute(
        """
        INSERT INTO proposals (hoa_id, creator_user_id, title, description, category, share_code, lat, lng, location_description)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (int(hoa_id), int(creator_user_id), title.strip(), description.strip(), category.strip(), share_code, lat, lng, location_description),
    )
    conn.commit()
    return int(cur.lastrowid)


def _row_to_proposal(row: sqlite3.Row) -> dict:
    keys = row.keys()
    return {
        "id": int(row["id"]),
        "hoa_id": int(row["hoa_id"]),
        "creator_user_id": int(row["creator_user_id"]),
        "title": str(row["title"]),
        "description": str(row["description"]),
        "category": str(row["category"]),
        "status": str(row["status"]),
        "share_code": str(row["share_code"]),
        "cosigner_count": int(row["cosigner_count"]),
        "upvote_count": int(row["upvote_count"]),
        "created_at": str(row["created_at"]) if row["created_at"] else None,
        "published_at": str(row["published_at"]) if row["published_at"] else None,
        "lat": float(row["lat"]) if "lat" in keys and row["lat"] is not None else None,
        "lng": float(row["lng"]) if "lng" in keys and row["lng"] is not None else None,
        "location_description": str(row["location_description"]) if "location_description" in keys and row["location_description"] else None,
    }


def get_proposal(conn: sqlite3.Connection, proposal_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM proposals WHERE id = ?", (int(proposal_id),)
    ).fetchone()
    if not row:
        return None
    return _row_to_proposal(row)


def get_proposal_by_share_code(conn: sqlite3.Connection, share_code: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM proposals WHERE share_code = ?", (share_code.upper(),)
    ).fetchone()
    if not row:
        return None
    return _row_to_proposal(row)


def list_proposals_for_hoa(
    conn: sqlite3.Connection, hoa_id: int, *, include_archived: bool = False
) -> list[dict]:
    if include_archived:
        rows = conn.execute(
            """
            SELECT * FROM proposals
            WHERE hoa_id = ? AND status IN ('public', 'archived')
            ORDER BY upvote_count DESC, created_at DESC
            """,
            (int(hoa_id),),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM proposals
            WHERE hoa_id = ? AND status = 'public'
            ORDER BY upvote_count DESC, created_at DESC
            """,
            (int(hoa_id),),
        ).fetchall()
    return [_row_to_proposal(r) for r in rows]


def list_proposals_for_user(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM proposals
        WHERE creator_user_id = ?
        ORDER BY created_at DESC
        """,
        (int(user_id),),
    ).fetchall()
    return [_row_to_proposal(r) for r in rows]


def get_active_proposal_for_user(conn: sqlite3.Connection, user_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT * FROM proposals
        WHERE creator_user_id = ? AND status IN ('private', 'public')
        LIMIT 1
        """,
        (int(user_id),),
    ).fetchone()
    if not row:
        return None
    return _row_to_proposal(row)


def archive_proposal(conn: sqlite3.Connection, proposal_id: int) -> None:
    conn.execute(
        "UPDATE proposals SET status = 'archived' WHERE id = ?",
        (int(proposal_id),),
    )
    conn.commit()


def archive_stale_proposals(conn: sqlite3.Connection, days: int = 60) -> int:
    cur = conn.execute(
        """
        UPDATE proposals
        SET status = 'archived'
        WHERE status = 'public'
          AND published_at IS NOT NULL
          AND published_at < datetime('now', ? || ' days')
        """,
        (f"-{days}",),
    )
    conn.commit()
    return cur.rowcount


def create_cosigner(conn: sqlite3.Connection, *, proposal_id: int, user_id: int) -> None:
    conn.execute(
        "INSERT INTO proposal_cosigners (proposal_id, user_id) VALUES (?, ?)",
        (int(proposal_id), int(user_id)),
    )
    conn.execute(
        "UPDATE proposals SET cosigner_count = cosigner_count + 1 WHERE id = ?",
        (int(proposal_id),),
    )
    # Check if threshold reached (2 co-signers = cosigner_count now 2)
    row = conn.execute("SELECT cosigner_count FROM proposals WHERE id = ?", (int(proposal_id),)).fetchone()
    if row and int(row["cosigner_count"]) >= 2:
        conn.execute(
            """
            UPDATE proposals
            SET status = 'public', published_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'private'
            """,
            (int(proposal_id),),
        )
    conn.commit()


def delete_cosigner(conn: sqlite3.Connection, *, proposal_id: int, user_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM proposal_cosigners WHERE proposal_id = ? AND user_id = ?",
        (int(proposal_id), int(user_id)),
    )
    if cur.rowcount == 0:
        conn.commit()
        return False
    conn.execute(
        "UPDATE proposals SET cosigner_count = MAX(0, cosigner_count - 1) WHERE id = ?",
        (int(proposal_id),),
    )
    row = conn.execute(
        "SELECT cosigner_count, status FROM proposals WHERE id = ?", (int(proposal_id),)
    ).fetchone()
    if row and int(row["cosigner_count"]) < 2 and str(row["status"]) == "public":
        conn.execute(
            "UPDATE proposals SET status = 'private', published_at = NULL WHERE id = ?",
            (int(proposal_id),),
        )
    conn.commit()
    return True


def get_cosigner(conn: sqlite3.Connection, proposal_id: int, user_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM proposal_cosigners WHERE proposal_id = ? AND user_id = ?",
        (int(proposal_id), int(user_id)),
    ).fetchone()
    return dict(row) if row else None


def list_cosigners(conn: sqlite3.Connection, proposal_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT user_id, created_at FROM proposal_cosigners WHERE proposal_id = ? ORDER BY created_at",
        (int(proposal_id),),
    ).fetchall()
    return [{"user_id": int(r["user_id"]), "cosigned_at": str(r["created_at"])} for r in rows]


def list_cosigner_names(conn: sqlite3.Connection, proposal_id: int) -> list[str]:
    """Return display names of cosigners for a proposal, ordered by sign date."""
    rows = conn.execute(
        """SELECT u.display_name
           FROM proposal_cosigners pc
           JOIN users u ON u.id = pc.user_id
           WHERE pc.proposal_id = ?
           ORDER BY pc.created_at""",
        (int(proposal_id),),
    ).fetchall()
    return [str(r["display_name"] or "A resident") for r in rows]


# ---------------------------------------------------------------------------
# HOA proxy status (from document ingestion)
# ---------------------------------------------------------------------------

def get_hoa_by_id(conn: sqlite3.Connection, hoa_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM hoas WHERE id = ?", (int(hoa_id),)).fetchone()
    return dict(row) if row else None


def set_hoa_proxy_status(
    conn: sqlite3.Connection,
    hoa_id: int,
    status: str,
    citation: str | None = None,
) -> None:
    """Update the HOA's proxy_status determined from governing document analysis."""
    conn.execute(
        "UPDATE hoas SET proxy_status = ?, proxy_citation = ? WHERE id = ?",
        (status, citation, int(hoa_id)),
    )
    conn.commit()


def get_proxy_by_verification_code(conn: sqlite3.Connection, code: str) -> dict | None:
    """Look up a signed proxy by its verification code. Returns None if not found."""
    row = conn.execute(
        """
        SELECT pa.*,
               gu.display_name AS grantor_name,
               du.display_name AS delegate_name,
               h.name AS hoa_name
        FROM proxy_assignments pa
        JOIN users gu ON gu.id = pa.grantor_user_id
        JOIN users du ON du.id = pa.delegate_user_id
        JOIN hoas h ON h.id = pa.hoa_id
        WHERE pa.verification_code = ?
        """,
        (code,),
    ).fetchone()
    return dict(row) if row else None


def create_upvote(conn: sqlite3.Connection, *, proposal_id: int, user_id: int) -> None:
    conn.execute(
        "INSERT INTO proposal_upvotes (proposal_id, user_id) VALUES (?, ?)",
        (int(proposal_id), int(user_id)),
    )
    conn.execute(
        "UPDATE proposals SET upvote_count = upvote_count + 1 WHERE id = ?",
        (int(proposal_id),),
    )
    conn.commit()


def delete_upvote(conn: sqlite3.Connection, *, proposal_id: int, user_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM proposal_upvotes WHERE proposal_id = ? AND user_id = ?",
        (int(proposal_id), int(user_id)),
    )
    if cur.rowcount == 0:
        conn.commit()
        return False
    conn.execute(
        "UPDATE proposals SET upvote_count = MAX(0, upvote_count - 1) WHERE id = ?",
        (int(proposal_id),),
    )
    conn.commit()
    return True


def get_upvote(conn: sqlite3.Connection, proposal_id: int, user_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM proposal_upvotes WHERE proposal_id = ? AND user_id = ?",
        (int(proposal_id), int(user_id)),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Cost Tracker
# ---------------------------------------------------------------------------

def log_api_usage(
    conn: sqlite3.Connection,
    *,
    service: str,
    operation: str,
    units: float,
    unit_type: str,
    est_cost_usd: float | None = None,
    metadata: dict | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO api_usage_log (service, operation, units, unit_type, est_cost_usd, metadata)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (service, operation, units, unit_type, est_cost_usd, json.dumps(metadata or {})),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_usage_summary(
    conn: sqlite3.Connection,
    *,
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Return aggregated usage grouped by service.

    Filter by month (YYYY-MM) or explicit date range.
    """
    if month:
        date_from = f"{month}-01"
        # Use first day of next month as exclusive upper bound
        parts = month.split("-")
        y, m = int(parts[0]), int(parts[1])
        m += 1
        if m > 12:
            m = 1
            y += 1
        date_to = f"{y:04d}-{m:02d}-01"

    clauses: list[str] = []
    params: list[Any] = []
    if date_from:
        clauses.append("timestamp >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("timestamp < ?")
        params.append(date_to)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT service, unit_type,
               SUM(units) AS total_units,
               SUM(est_cost_usd) AS total_est_cost_usd
        FROM api_usage_log
        {where}
        GROUP BY service
        ORDER BY total_est_cost_usd DESC
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def get_usage_daily(
    conn: sqlite3.Connection,
    *,
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    if month:
        date_from = f"{month}-01"
        parts = month.split("-")
        y, m = int(parts[0]), int(parts[1])
        m += 1
        if m > 12:
            m = 1
            y += 1
        date_to = f"{y:04d}-{m:02d}-01"

    clauses: list[str] = []
    params: list[Any] = []
    if date_from:
        clauses.append("timestamp >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("timestamp < ?")
        params.append(date_to)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT date(timestamp) AS day, service,
               SUM(units) AS total_units,
               SUM(est_cost_usd) AS total_est_cost_usd
        FROM api_usage_log
        {where}
        GROUP BY day, service
        ORDER BY day, service
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def create_fixed_cost(
    conn: sqlite3.Connection,
    *,
    service: str,
    description: str | None = None,
    amount_usd: float,
    frequency: str = "monthly",
) -> int:
    monthly_equiv = amount_usd if frequency == "monthly" else round(amount_usd / 12, 2)
    cur = conn.execute(
        """
        INSERT INTO fixed_costs (service, description, amount_usd, frequency, monthly_equiv)
        VALUES (?, ?, ?, ?, ?)
        """,
        (service, description, amount_usd, frequency, monthly_equiv),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_fixed_cost(
    conn: sqlite3.Connection,
    cost_id: int,
    *,
    service: str | None = None,
    description: str | None = None,
    amount_usd: float | None = None,
    frequency: str | None = None,
    active: bool | None = None,
) -> dict | None:
    row = conn.execute("SELECT * FROM fixed_costs WHERE id = ?", (int(cost_id),)).fetchone()
    if not row:
        return None
    current = dict(row)
    svc = service if service is not None else current["service"]
    desc = description if description is not None else current["description"]
    amt = amount_usd if amount_usd is not None else current["amount_usd"]
    freq = frequency if frequency is not None else current["frequency"]
    act = int(active) if active is not None else current["active"]
    monthly = amt if freq == "monthly" else round(amt / 12, 2)
    conn.execute(
        """
        UPDATE fixed_costs
        SET service = ?, description = ?, amount_usd = ?, frequency = ?,
            monthly_equiv = ?, active = ?,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE id = ?
        """,
        (svc, desc, amt, freq, monthly, act, int(cost_id)),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM fixed_costs WHERE id = ?", (int(cost_id),)).fetchone()
    return dict(updated) if updated else None


def delete_fixed_cost(conn: sqlite3.Connection, cost_id: int) -> bool:
    conn.execute(
        """
        UPDATE fixed_costs SET active = 0,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE id = ?
        """,
        (int(cost_id),),
    )
    conn.commit()
    return True


def list_fixed_costs(conn: sqlite3.Connection, active_only: bool = True) -> list[dict]:
    if active_only:
        rows = conn.execute(
            "SELECT * FROM fixed_costs WHERE active = 1 ORDER BY monthly_equiv DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM fixed_costs ORDER BY active DESC, monthly_equiv DESC"
        ).fetchall()
    return [dict(r) for r in rows]
