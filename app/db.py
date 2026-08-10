"""Three tables. That's the whole database.

    users       — accounts, preferences, AeroAPI key and spend counters
    flights     — ONE ROW PER LEG. Every fact about that flight, in a
                  named column, whether it came from ADS-B or the airline.
    positions   — the breadcrumb trail, keyed by FLIGHT not by user

Before v5.0 this was seven tables. A single leg's story was spread across
`legs` (the schedule), `flight_aircraft` (what ADS-B had seen),
`flight_enrichment` (a JSON blob of what the airline said) and
`flight_closeout` (another JSON blob). Nothing owned the flight, so four
modules each reached into their own table and `compute_live_payload`
reconciled the pieces at DISPLAY time, on every page render, for every
viewer. That reconciliation is where the ordering bugs lived.

Now the poller decides once and writes it down; everything else reads the
row. Two more tables, `aircraft` and `positions` (the old user-scoped
one), were dead — nothing had read or written them in several versions.

WHY ADS-B AND AIRLINE VALUES SIT IN SEPARATE COLUMNS
-----------------------------------------------------
`off_actual_api` and `off_observed` are both "when the wheels came off".
Keeping them apart means the card can say WHICH it's showing, the two can
be compared when they disagree, and a lagging airline record can never
silently overwrite something we watched happen. Merging them into one
column would throw away the disagreement, which is the interesting part.

MIGRATION
---------
`users` and the schedule carry over. Breadcrumb tracks carry over. The old
enrichment and closeout blobs do NOT — they were 30-day data in a shape
that no longer exists, and parsing them into columns would be a one-off
guess at fields we can simply re-fetch. Past flights keep their route and
their flown path; their gate times start over.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import List

# Overridable so a test run can point at a scratch file instead of the real
# database. Production ignores it and uses data/flighttracker.db.
DB_FILE = Path(os.environ.get(
    "PT_DB_FILE",
    Path(__file__).resolve().parent.parent / "data" / "flighttracker.db"))


def get_connection() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE), timeout=20.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


# Every column on `flights` beyond the primary key, as (name, type).
# Declared in one list so a new field is added in exactly one place and
# the migration below picks it up automatically.
FLIGHT_COLUMNS: List[tuple] = [
    # ---- schedule, from the FFDO paste. Never overwritten by live data.
    ("sort_index",            "INTEGER NOT NULL DEFAULT 0"),
    ("date",                  "TEXT"),
    ("flight_number",         "TEXT"),
    ("origin",                "TEXT"),
    ("destination",           "TEXT"),
    ("dep_time_local",        "TEXT"),
    ("arr_time_local",        "TEXT"),
    ("is_deadhead",           "INTEGER NOT NULL DEFAULT 0"),
    ("trip_start",            "INTEGER NOT NULL DEFAULT 0"),
    # Which carrier actually operates this leg. A deadhead's FFDO line
    # gives a bare number, so ENY is often wrong. Resolved once, stored.
    ("operator_callsign",     "TEXT"),
    ("fa_flight_id",          "TEXT"),

    # ---- aircraft identity. The hex is the lock that stops a turn's
    # return flight being mistaken for the outbound. Written once.
    ("aircraft_hex",          "TEXT"),
    ("aircraft_acquired_at",  "TEXT"),
    ("tail_adsb",             "TEXT"),
    ("tail_api",              "TEXT"),
    ("type_code",             "TEXT"),
    ("aircraft_type",         "TEXT"),

    # ---- latest live position. ADS-B only. Overwritten each poll, but a
    # blank never overwrites a known value.
    ("last_lat",              "REAL"),
    ("last_lon",              "REAL"),
    ("last_on_ground",        "INTEGER"),
    ("last_altitude_ft",      "INTEGER"),
    ("last_speed_kts",        "INTEGER"),
    ("last_track",            "REAL"),
    ("last_squawk",           "TEXT"),
    ("last_fix_age_s",        "REAL"),
    ("last_signal_at",        "TEXT"),

    # ---- the flight-cycle state machine, from ADS-B
    ("airborne_seen",         "INTEGER NOT NULL DEFAULT 0"),
    ("landed_seen",           "INTEGER NOT NULL DEFAULT 0"),
    ("landing_since",         "TEXT"),
    ("stopped_since",         "TEXT"),
    ("relaunched",            "INTEGER NOT NULL DEFAULT 0"),

    # ---- the four events, doubled. "_api" is the airline's own figure,
    # "_observed" is what we watched happen. Both written once.
    ("out_actual_api",        "TEXT"),
    ("out_observed",          "TEXT"),
    ("off_actual_api",        "TEXT"),
    ("off_observed",          "TEXT"),
    ("on_actual_api",         "TEXT"),
    ("on_observed",           "TEXT"),
    ("in_actual_api",         "TEXT"),
    ("in_observed",           "TEXT"),

    # ---- the airline's forecasts. Overwritten as they move.
    ("out_estimated",         "TEXT"),
    ("off_estimated",         "TEXT"),
    ("on_estimated",          "TEXT"),
    ("in_estimated",          "TEXT"),
    # ---- the airline's published schedule, snapshotted the first time we
    # see it. Airlines amend published times; without this, "was 11:55"
    # becomes unanswerable.
    ("out_scheduled",         "TEXT"),
    ("off_scheduled",         "TEXT"),
    ("on_scheduled",          "TEXT"),
    ("in_scheduled",          "TEXT"),

    # ---- airline-only facts
    ("gate_origin",           "TEXT"),
    ("gate_destination",      "TEXT"),
    ("terminal_origin",       "TEXT"),
    ("terminal_destination",  "TEXT"),
    ("baggage_claim",         "TEXT"),
    ("cancelled",             "INTEGER NOT NULL DEFAULT 0"),
    ("diverted",              "INTEGER NOT NULL DEFAULT 0"),
    ("blocked",               "INTEGER NOT NULL DEFAULT 0"),
    ("status_text",           "TEXT"),
    # Where it ACTUALLY went. Only differs from `destination` on a
    # diversion, and the airline is the only source that knows.
    ("destination_actual",    "TEXT"),

    # ---- the two pills
    # phase_tag only ever moves forward; status_tag moves both ways
    # except Cancelled and Diverted, which stick.
    ("phase_tag",             "TEXT"),
    ("phase_tag_at",          "TEXT"),
    ("status_tag",            "TEXT"),
    ("status_tag_at",         "TEXT"),
    # Minutes the AIRLINE has moved its own times. Drives the Delayed
    # pill. Distinct from the variance columns below, which drive the
    # "12 min late" note and never affect the pill.
    ("dep_revision_min",      "INTEGER"),
    ("arr_revision_min",      "INTEGER"),
    ("out_variance_min",      "INTEGER"),
    ("in_variance_min",       "INTEGER"),

    # ---- derived, recomputed each poll
    ("progress_pct",          "REAL"),
    ("ete_min",               "REAL"),
    ("distance_nm",           "REAL"),

    # ---- closure
    ("closed",                "INTEGER NOT NULL DEFAULT 0"),
    ("closed_at",             "TEXT"),
    ("closed_by",             "TEXT"),
    ("arrival_source",        "TEXT"),

    # ---- bookkeeping
    ("api_queries_used",      "INTEGER NOT NULL DEFAULT 0"),
    ("closeout_tries",        "INTEGER NOT NULL DEFAULT 0"),
    ("fallback_tries",        "INTEGER NOT NULL DEFAULT 0"),
    ("delay_watch_tries",     "INTEGER NOT NULL DEFAULT 0"),
    ("last_api_query_at",     "TEXT"),
    ("last_api_reason",       "TEXT"),
    ("api_raw",               "TEXT"),
    ("last_polled_at",        "TEXT"),
    ("created_at",            "TEXT"),
    # Set when the leg closes. One indexed delete does the 30-day cleanup.
    ("purge_after",           "TEXT"),
]


def _create_flights(conn) -> None:
    cols = ",\n                ".join(f"{n} {t}" for n, t in FLIGHT_COLUMNS)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS flights (
            id TEXT NOT NULL,
            user_id INTEGER NOT NULL DEFAULT 0,
            {cols},
            PRIMARY KEY (user_id, id)
        )
        """
    )
    # Leg ids are derived from the flight itself, so two pilots on the
    # same flight generate the same id. (user_id, id) rather than id alone
    # is what stops one pilot's import silently replacing another's rows.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_flights_user ON flights(user_id, sort_index)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_flights_id ON flights(id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_flights_number_date ON flights(flight_number, date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_flights_purge ON flights(purge_after)")


def _sync_flight_columns(conn) -> None:
    """Add any column in FLIGHT_COLUMNS that the table doesn't have yet.

    Adding a field means appending one line to the list above; this picks
    it up on next boot. SQLite can't add a NOT NULL column without a
    default, which every entry in the list already supplies.
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(flights)")}
    for name, decl in FLIGHT_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE flights ADD COLUMN {name} {decl}")


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
                recovery_code_hash TEXT,
                time_format TEXT DEFAULT '24',
                theme TEXT DEFAULT 'dark',
                poll_seconds INTEGER DEFAULT 45,
                show_flightaware INTEGER DEFAULT 1,
                show_fr24 INTEGER DEFAULT 1,
                is_admin INTEGER DEFAULT 0,
                aeroapi_enabled INTEGER NOT NULL DEFAULT 0,
                aeroapi_key TEXT,
                aeroapi_queries INTEGER NOT NULL DEFAULT 0,
                aeroapi_budget REAL NOT NULL DEFAULT 4.50,
                aeroapi_reported_cost REAL,
                aeroapi_reported_calls INTEGER,
                aeroapi_usage_at TEXT,
                aeroapi_period TEXT,
                created_at TEXT
            )
            """
        )
        # Upgrades from before these columns existed.
        ucols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        for name, decl in [
            ("recovery_code_hash", "TEXT"),
            ("is_admin", "INTEGER DEFAULT 0"),
            ("aeroapi_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("aeroapi_key", "TEXT"),
            ("aeroapi_queries", "INTEGER NOT NULL DEFAULT 0"),
            ("aeroapi_budget", "REAL NOT NULL DEFAULT 4.50"),
            ("aeroapi_reported_cost", "REAL"),
            ("aeroapi_reported_calls", "INTEGER"),
            ("aeroapi_usage_at", "TEXT"),
            ("aeroapi_period", "TEXT"),
            ("show_flightaware", "INTEGER DEFAULT 1"),
            ("show_fr24", "INTEGER DEFAULT 1"),
        ]:
            if name not in ucols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")
        if "is_admin" not in ucols:
            # Whoever already exists (the original single pilot) becomes
            # admin automatically on upgrade.
            conn.execute("UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users)")

        _create_flights(conn)
        _sync_flight_columns(conn)

        # v4 had a DEAD `positions` table left over from the OpenSky era,
        # user-scoped and with a completely different shape. v5 reuses the
        # name for the breadcrumb trail, and CREATE TABLE IF NOT EXISTS
        # would silently do nothing against the old one — leaving every
        # write to fail on a missing column. Nothing has read or written
        # the old table in several versions, so it goes.
        if _table_exists(conn, "positions"):
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(positions)")}
            if "flight_key" not in cols:
                conn.execute("DROP TABLE positions")
                print("[db] dropped the dead v4 positions table")

        # Breadcrumbs. Keyed by FLIGHT, not by user: a track is a fact
        # about an aeroplane, not about a person, so two crew on the same
        # leg share one path instead of storing it twice. The key is the
        # leg id with any "-DH" suffix stripped, so a deadhead and a
        # working leg on the same flight record into the same path.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                flight_key TEXT NOT NULL,
                ts TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                on_ground INTEGER,
                PRIMARY KEY (flight_key, ts)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_key_ts ON positions(flight_key, ts)")

        _migrate_from_v4(conn)
        conn.commit()
    finally:
        conn.close()


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _migrate_from_v4(conn) -> None:
    """Carry the schedule and the flown tracks over from the old schema.

    Deliberately partial. The schedule is irreplaceable — it was typed in
    — and tracks are irreplaceable, they were observed. The airline
    enrichment and closeout blobs are neither: they are at most 30 days
    old, they can be re-fetched, and mapping two nested JSON documents
    into sixty columns is a one-off guess. Past flights keep their route
    and their path; their gate times start over.
    """
    if _table_exists(conn, "flight_tracks"):
        already = conn.execute("SELECT COUNT(*) c FROM positions").fetchone()["c"]
        if not already:
            conn.execute(
                "INSERT OR IGNORE INTO positions (flight_key, ts, lat, lon, on_ground) "
                "SELECT flight_key, ts, lat, lon, on_ground FROM flight_tracks"
            )
            moved = conn.execute("SELECT COUNT(*) c FROM positions").fetchone()["c"]
            if moved:
                print(f"[db] carried {moved} track points over from v4")

    if _table_exists(conn, "legs"):
        already = conn.execute("SELECT COUNT(*) c FROM flights").fetchone()["c"]
        if not already:
            old = {r["name"] for r in conn.execute("PRAGMA table_info(legs)")}
            has_op = "operator_callsign" in old
            has_trip = "trip_start" in old
            rows = conn.execute("SELECT * FROM legs").fetchall()
            for r in rows:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO flights "
                        "(id, user_id, sort_index, date, flight_number, origin, destination, "
                        " dep_time_local, arr_time_local, is_deadhead, trip_start, "
                        " operator_callsign) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (r["id"], r["user_id"] or 0, r["sort_index"], r["date"],
                         r["flight_number"], r["origin"], r["destination"],
                         r["dep_time_local"], r["arr_time_local"], r["is_deadhead"],
                         (r["trip_start"] if has_trip else 0),
                         (r["operator_callsign"] if has_op else None)),
                    )
                except Exception:
                    continue
            n = conn.execute("SELECT COUNT(*) c FROM flights").fetchone()["c"]
            if n:
                print(f"[db] carried {n} schedule legs over from v4")

    # The old tables are left in place rather than dropped. If anything
    # about the migration turns out wrong, the original data is still
    # there to look at; dropping it would make that unrecoverable.
    for dead in ("aircraft",):
        if _table_exists(conn, dead):
            conn.execute(f"DROP TABLE {dead}")
