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
                is_admin INTEGER DEFAULT 0,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS legs (
                id TEXT NOT NULL,
                user_id INTEGER NOT NULL DEFAULT 0,
                sort_index INTEGER NOT NULL,
                date TEXT NOT NULL,
                flight_number TEXT NOT NULL,
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                dep_time_local TEXT NOT NULL,
                arr_time_local TEXT NOT NULL,
                is_deadhead INTEGER NOT NULL DEFAULT 0,
                trip_start INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, id)
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
        if "trip_start" not in leg_cols:
            conn.execute("ALTER TABLE legs ADD COLUMN trip_start INTEGER NOT NULL DEFAULT 0")
        pos_cols = [r["name"] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
        if "user_id" not in pos_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN user_id INTEGER")

        # Migration: admin flag for basic account-management tools in Settings.
        user_cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "is_admin" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
            # Whoever already exists (the original single pilot, pre-multi-user)
            # becomes admin automatically on upgrade.
            conn.execute("UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users)")
        if "recovery_code_hash" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN recovery_code_hash TEXT")

        # Optional per-pilot AeroAPI credentials. Each pilot brings their own
        # key and pays their own (usually zero) bill, exactly as the OpenSky
        # fields used to work. With no key the app behaves as it always has.
        if "aeroapi_enabled" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN aeroapi_enabled INTEGER NOT NULL DEFAULT 0")
        if "aeroapi_key" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN aeroapi_key TEXT")
        # Query accounting, so the pilot can see what their key is spending
        # before a bill tells them. Reset when the month rolls over.
        if "aeroapi_queries" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN aeroapi_queries INTEGER NOT NULL DEFAULT 0")
        if "aeroapi_period" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN aeroapi_period TEXT")

        # Migration: legs are keyed on (user_id, id), not id alone.
        #
        # Leg ids are derived from the flight itself ("2026-08-04-3729-DFW-OKC"),
        # so two pilots who fly the same flight on the same day generate the
        # SAME id. With `id` as a lone PRIMARY KEY, the INSERT OR REPLACE in
        # save_schedule() silently reassigned the first pilot's rows to the
        # second — real, unreported data loss as soon as a second account
        # imported an overlapping schedule.
        #
        # Leg id VALUES are deliberately left alone. Every positions query is
        # already user-scoped, and the ids appear in "/?leg=<id>" URLs, so
        # rewriting them would break bookmarks and in-flight links for no gain.
        # SQLite can't ALTER a primary key, hence the table rebuild.
        legs_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='legs'"
        ).fetchone()
        if legs_sql and "PRIMARY KEY (user_id, id)" not in (legs_sql["sql"] or ""):
            # Orphaned rows (pre-multi-user data) get the same treatment they
            # already got elsewhere: attached to the earliest account, or
            # parked on sentinel 0 if no account exists yet, where
            # claim_orphaned_data() will pick them up at first registration.
            conn.execute(
                "UPDATE legs SET user_id = COALESCE((SELECT MIN(id) FROM users), 0) "
                "WHERE user_id IS NULL"
            )
            conn.execute(
                """
                CREATE TABLE legs_rebuild (
                    id TEXT NOT NULL,
                    user_id INTEGER NOT NULL DEFAULT 0,
                    sort_index INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    flight_number TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    dep_time_local TEXT NOT NULL,
                    arr_time_local TEXT NOT NULL,
                    is_deadhead INTEGER NOT NULL DEFAULT 0,
                    trip_start INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, id)
                )
                """
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO legs_rebuild
                    (id, user_id, sort_index, date, flight_number, origin,
                     destination, dep_time_local, arr_time_local, is_deadhead, trip_start)
                SELECT id, COALESCE(user_id, 0), sort_index, date, flight_number,
                       origin, destination, dep_time_local, arr_time_local,
                       is_deadhead, COALESCE(trip_start, 0)
                FROM legs
                """
            )
            conn.execute("DROP TABLE legs")
            conn.execute("ALTER TABLE legs_rebuild RENAME TO legs")

        # Added after the rebuild above on purpose: that migration copies an
        # explicit column list, so a column added before it would be dropped.
        leg_cols_now = {r["name"] for r in conn.execute("PRAGMA table_info(legs)")}
        if "operator_callsign" not in leg_cols_now:
            conn.execute("ALTER TABLE legs ADD COLUMN operator_callsign TEXT")

        # Every positions lookup filters on user_id as well as leg_id; the
        # original index covered only (leg_id, ts).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_positions_user_leg ON positions(user_id, leg_id, ts)"
        )

        # Flight tracks are keyed by the FLIGHT, not by the user.
        #
        # A track is a fact about an aircraft, not about a person: if two
        # pilots are on ENY3729 on the same day, that's one aeroplane and
        # one path. The older `positions` table carried a user_id and kept
        # a private copy per account, so N pilots meant N identical tracks
        # and the background poller would write the same points repeatedly.
        #
        # The flight key is the leg id with any "-DH" suffix stripped, so a
        # deadhead and a working leg on the same flight share one track
        # rather than splitting into two half-recorded ones.
        #
        # Nothing leaks between accounts: the UI only looks up a track for
        # a leg already on that user's own schedule, and which flights a
        # user is on lives in `legs`, which stays user-scoped.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS flight_tracks (
                flight_key TEXT NOT NULL,
                ts TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                on_ground INTEGER,
                PRIMARY KEY (flight_key, ts)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_flight_tracks_key_ts ON flight_tracks(flight_key, ts)"
        )

        # Which physical aircraft is flying a given leg.
        #
        # A callsign is not unique to a leg — regional turns fly out and
        # back under one flight number — but an aircraft's ICAO hex address
        # is unique. Once an aircraft has been seen at the leg's ORIGIN it
        # is recorded here, and from then on only that hex is accepted for
        # the leg. That's what makes diversions safe: identity never
        # depends on where the aircraft is going.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS flight_aircraft (
                flight_key TEXT PRIMARY KEY,
                icao24 TEXT NOT NULL,
                acquired_at TEXT NOT NULL
            )
            """
        )
        fa_cols = {r["name"] for r in conn.execute("PRAGMA table_info(flight_aircraft)")}
        # Whether this aircraft has actually flown yet — without it, sitting
        # on the ground before pushback looks identical to sitting on the
        # ground after landing.
        if "airborne_seen" not in fa_cols:
            conn.execute("ALTER TABLE flight_aircraft ADD COLUMN airborne_seen INTEGER NOT NULL DEFAULT 0")
        # When it last came to a complete stop on the ground.
        if "stopped_since" not in fa_cols:
            conn.execute("ALTER TABLE flight_aircraft ADD COLUMN stopped_since TEXT")
        # Last observed transponder code, ONLY ever compared while stopped
        # on the ground — codes are routinely reassigned in flight as an
        # aircraft is handed between ATC facilities, so an airborne change
        # means nothing about whether the flight has ended.
        if "last_squawk" not in fa_cols:
            conn.execute("ALTER TABLE flight_aircraft ADD COLUMN last_squawk TEXT")
        # Set once the leg is judged finished; blocks any re-acquisition.
        if "completed_at" not in fa_cols:
            conn.execute("ALTER TABLE flight_aircraft ADD COLUMN completed_at TEXT")

        # Cached AeroAPI enrichment, one row per leg per pilot.
        #
        # Scoped to the pilot whose key paid for it, unlike flight_tracks
        # which is shared: position is public ADS-B, but this was bought
        # with one person's key under a personal-use tier, so it isn't
        # pooled across accounts.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS flight_enrichment (
                leg_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                fetched_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (leg_id, user_id)
            )
            """
        )
        enr_cols = {r["name"] for r in conn.execute("PRAGMA table_info(flight_enrichment)")}
        # The full untouched API record. Every query costs the pilot money,
        # so throwing away fields we don't currently render would mean
        # paying again later to re-fetch data we already had.
        if "raw" not in enr_cols:
            conn.execute("ALTER TABLE flight_enrichment ADD COLUMN raw TEXT")
        # The first values we ever saw for this leg. Airlines amend
        # published schedules, so without a snapshot the original times are
        # simply lost and "was 11:55" becomes unanswerable.
        if "first_seen" not in enr_cols:
            conn.execute("ALTER TABLE flight_enrichment ADD COLUMN first_seen TEXT")

        # One-time migration of per-user history already recorded.
        # INSERT OR IGNORE collapses the duplicate (flight_key, ts) rows
        # that two accounts watching the same flight would have produced.
        already_migrated = conn.execute("SELECT COUNT(*) AS c FROM flight_tracks").fetchone()["c"]
        if not already_migrated:
            conn.execute(
                """
                INSERT OR IGNORE INTO flight_tracks (flight_key, ts, lat, lon, on_ground)
                SELECT
                    CASE WHEN leg_id LIKE '%-DH'
                         THEN substr(leg_id, 1, length(leg_id) - 3)
                         ELSE leg_id END,
                    ts, lat, lon, on_ground
                FROM positions
                ORDER BY ts
                """
            )
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
                        (id, user_id, sort_index, date, flight_number, origin, destination,
                         dep_time_local, arr_time_local, is_deadhead)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["id"],
                        # Sentinel 0 = not owned yet. The first account created
                        # claims these via auth.claim_orphaned_data().
                        0,
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
