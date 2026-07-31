"""Aircraft table: tracks which icao24 addresses we've seen and lets the
pilot fill in registration / type in the admin page (no reliable free
metadata API exists, so this is self-maintained rather than auto-fetched)."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Dict, Any

from .db import get_connection


def note_aircraft_seen(icao24: Optional[str]) -> None:
    """Upsert a bare row the first time we see an icao24 via live tracking,
    and bump last_seen on subsequent sightings."""
    if not icao24:
        return
    icao24 = icao24.strip().lower()
    if not icao24:
        return
    now = datetime.utcnow().isoformat() + "Z"
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO aircraft (icao24, first_seen, last_seen)
            VALUES (?, ?, ?)
            ON CONFLICT(icao24) DO UPDATE SET last_seen = excluded.last_seen
            """,
            (icao24, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_aircraft_info(icao24: Optional[str]) -> Optional[Dict[str, Any]]:
    if not icao24:
        return None
    icao24 = icao24.strip().lower()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM aircraft WHERE icao24 = ?", (icao24,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return dict(row)


def list_aircraft() -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM aircraft ORDER BY last_seen DESC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def update_aircraft(icao24: str, registration: str, aircraft_type: str, notes: str) -> None:
    icao24 = icao24.strip().lower()
    if not icao24:
        return
    now = datetime.utcnow().isoformat() + "Z"
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO aircraft (icao24, registration, aircraft_type, notes, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(icao24) DO UPDATE SET
                registration = excluded.registration,
                aircraft_type = excluded.aircraft_type,
                notes = excluded.notes
            """,
            (icao24, registration.strip(), aircraft_type.strip(), notes.strip(), now, now),
        )
        conn.commit()
    finally:
        conn.close()
