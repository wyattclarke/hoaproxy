"""Participation tracking module for HOAproxy.

Provides functions for recording meeting attendance/voting data and
computing the "magic number" — how many proxies could swing a typical vote.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any


def add_participation_record(
    conn: sqlite3.Connection,
    hoa_id: int,
    meeting_date: str,
    meeting_type: str,
    total_units: int,
    votes_cast: int,
    quorum_required: int | None = None,
    quorum_met: bool | None = None,
    entered_by_user_id: int | None = None,
    notes: str | None = None,
) -> int:
    """Insert a participation record and return its id."""
    cur = conn.execute(
        """
        INSERT INTO participation_records
            (hoa_id, meeting_date, meeting_type, total_units, votes_cast,
             quorum_required, quorum_met, entered_by_user_id, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            hoa_id,
            meeting_date,
            meeting_type,
            total_units,
            votes_cast,
            quorum_required,
            quorum_met,
            entered_by_user_id,
            notes,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]


def get_participation_records(
    conn: sqlite3.Connection,
    hoa_id: int,
) -> list[dict[str, Any]]:
    """Return all participation records for an HOA, newest first."""
    rows = conn.execute(
        """
        SELECT id, hoa_id, meeting_date, meeting_type, total_units, votes_cast,
               quorum_required, quorum_met, source_document_id, entered_by_user_id,
               notes, created_at
        FROM participation_records
        WHERE hoa_id = ?
        ORDER BY meeting_date DESC, id DESC
        """,
        (hoa_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def calculate_magic_number(
    conn: sqlite3.Connection,
    hoa_id: int,
) -> dict[str, Any]:
    """Compute participation statistics and the proxy magic number.

    Returns a dict with:
      average_participation_rate  – float (0-1)
      average_votes_cast          – int
      total_units                 – int (from most recent record)
      proxies_to_swing            – ceil(average_votes_cast / 2) + 1
      data_points                 – int
      confidence                  – "low" | "medium" | "high"
    """
    records = get_participation_records(conn, hoa_id)
    data_points = len(records)

    if data_points == 0:
        return {
            "average_participation_rate": 0.0,
            "average_votes_cast": 0,
            "total_units": 0,
            "proxies_to_swing": 0,
            "data_points": 0,
            "confidence": "low",
        }

    # Most recent record's total_units
    total_units = int(records[0]["total_units"] or 0)

    # Average votes cast (only records where votes_cast is set)
    valid = [r for r in records if r["votes_cast"] is not None]
    if valid:
        avg_votes = sum(int(r["votes_cast"]) for r in valid) / len(valid)
    else:
        avg_votes = 0.0

    # Average participation rate (votes_cast / total_units per record)
    rates = []
    for r in valid:
        tu = int(r["total_units"] or 0)
        if tu > 0:
            rates.append(int(r["votes_cast"]) / tu)
    avg_rate = sum(rates) / len(rates) if rates else 0.0

    proxies_to_swing = math.ceil(avg_votes / 2) + 1

    if data_points < 3:
        confidence = "low"
    elif data_points <= 5:
        confidence = "medium"
    else:
        confidence = "high"

    return {
        "average_participation_rate": round(avg_rate, 4),
        "average_votes_cast": round(avg_votes),
        "total_units": total_units,
        "proxies_to_swing": proxies_to_swing,
        "data_points": data_points,
        "confidence": confidence,
    }
