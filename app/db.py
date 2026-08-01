"""Shared SQLite connection + schema for schedule, aircraft, and position data."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

DB_FILE = Path(__file__).resolve().parent.parent / "data" / "flighttracker.db"
LEGACY_SCHEDULE_JSON = Path(__file__).resolve().parent.parent / "data" / "schedule.json"


def get_connection() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS legs (
                id TEXT PRIMARY KEY,
                sort_index INTEGER NOT NULL,
                date TEXT NOT NULL,
                flight_number TEXT NOT NULL,
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                dep_time_local TEXT NOT NULL,
                arr_time_local TEXT NOT NULL,
                is_deadhead INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS aircraft (
                icao24 TEXT PRIMARY KEY,
                registration TEXT,
                aircraft_type TEXT,
                notes TEXT,
                first_seen TEXT,
                last_seen TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                leg_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_positions_leg ON positions(leg_id, ts)"
        )
        # Migration: on_ground wasn't in the original schema. Needed to tell
        # taxi-out/taxi-in/arrived apart from in-air breadcrumb points.
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
        if "on_ground" not in cols:
            conn.execute("ALTER TABLE positions ADD COLUMN on_ground INTEGER")
        conn.commit()
    finally:
        conn.close()

    _migrate_legacy_json_if_needed()


def _migrate_legacy_json_if_needed() -> None:
    """One-time import of the old data/schedule.json into SQLite, so upgrading
    doesn't lose an existing pasted schedule. No-ops once legs already exist."""
    if not LEGACY_SCHEDULE_JSON.exists():
        return
    conn = get_connection()
    try:
        existing = conn.execute("SELECT COUNT(*) AS c FROM legs").fetchone()["c"]
        if existing:
            return
        try:
            data = json.loads(LEGACY_SCHEDULE_JSON.read_text())
        except Exception:
            return
        legs = data.get("legs", [])
        if not legs:
            return
        for idx, item in enumerate(legs):
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO legs
                        (id, sort_index, date, flight_number, origin, destination,
                         dep_time_local, arr_time_local, is_deadhead)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["id"],
                        idx,
                        item["date"],
                        item["flight_number"],
                        item["origin"],
                        item["destination"],
                        item["dep_time_local"],
                        item["arr_time_local"],
                        1 if item.get("is_deadhead") else 0,
                    ),
                )
            except Exception:
                continue
        conn.commit()
    finally:
        conn.close()
