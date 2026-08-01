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
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                email TEXT,
                share_code TEXT UNIQUE,
                opensky_client_id TEXT DEFAULT '',
                opensky_client_secret TEXT DEFAULT '',
                time_format TEXT DEFAULT '24',
                theme TEXT DEFAULT 'dark',
                poll_seconds INTEGER DEFAULT 45,
                show_flightaware INTEGER DEFAULT 1,
                show_fr24 INTEGER DEFAULT 1,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS legs (
                id TEXT PRIMARY KEY,
                user_id INTEGER,
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

        # Migration: aircraft metadata is now looked up automatically from
        # OpenSky's public aircraft database instead of hand-entered.
        ac_cols = [r["name"] for r in conn.execute("PRAGMA table_info(aircraft)").fetchall()]
        for col in ("manufacturer", "model", "typecode", "lookup_attempted_at"):
            if col not in ac_cols:
                conn.execute(f"ALTER TABLE aircraft ADD COLUMN {col} TEXT")

        # Migration: multi-user groundwork. Legs/positions predate the users
        # table, so existing rows have no owner yet — they get attached to
        # the first account created (see auth.claim_orphaned_data).
        leg_cols = [r["name"] for r in conn.execute("PRAGMA table_info(legs)").fetchall()]
        if "user_id" not in leg_cols:
            conn.execute("ALTER TABLE legs ADD COLUMN user_id INTEGER")
        pos_cols = [r["name"] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
        if "user_id" not in pos_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN user_id INTEGER")
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
