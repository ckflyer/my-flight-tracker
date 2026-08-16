"""Four tables. Two of them are large; two are tiny.

    users       — accounts, preferences, AeroAPI key and spend counters
    flights     — ONE ROW PER REAL-WORLD FLIGHT, SHARED BY ALL CREW.
                  Every fact about it, in a named column, whether it came
                  from ADS-B or the airline. NOT scoped to a user.
    roster      — which flights are on whose schedule. Per-user: position
                  in the list, deadhead or working, trip boundary.
    positions   — the breadcrumb trail, keyed by flight

WHY `flights` IS SHARED (v5.1)
------------------------------
Crew fly together. When a captain and an FO are both using this app on the
same leg, that is ONE aeroplane, ONE takeoff, ONE gate-in. v5.0 gave them a
row each and wrote observed facts to both, which worked but meant two
AeroAPI queries for one flight — each pilot's key paying separately for an
identical answer. One row means one query serves everyone, and the two
views cannot drift apart.

The split is: anything true of the AEROPLANE lives on `flights`; anything
true of a PERSON'S RELATIONSHIP to it lives on `roster`. Deadheading is the
clearest case — the same flight is a working leg for one pilot and a
deadhead for another, so `is_deadhead` is a roster fact, not a flight fact.

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

from .version import SCHEMA_VERSION

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
    # ---- the flight itself, from the FFDO paste. Never overwritten by
    # live data. These are facts about the FLIGHT, so they are shared;
    # sort_index / is_deadhead / trip_start are per-person and live on
    # `roster` instead.
    ("date",                  "TEXT"),
    ("flight_number",         "TEXT"),
    ("origin",                "TEXT"),
    ("destination",           "TEXT"),
    ("dep_time_local",        "TEXT"),
    ("arr_time_local",        "TEXT"),
    # Which carrier actually operates this leg. A deadhead's FFDO line
    # gives a bare number, so the home prefix is often wrong. Resolved once,
    # stored. See carriers.py.
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
    # Dead as of v5.2 — each capped one of the six triggers the ticket
    # rule replaced. Kept because migrations here are append-only and
    # dropping a column in SQLite means rebuilding the table for nothing.
    ("closeout_tries",        "INTEGER NOT NULL DEFAULT 0"),
    ("fallback_tries",        "INTEGER NOT NULL DEFAULT 0"),
    ("delay_watch_tries",     "INTEGER NOT NULL DEFAULT 0"),
    # How many PAID attempts have been made to work out who operates this
    # deadhead, and when the last one was. Without these a failed lookup
    # left no trace, so the poller asked again 20 seconds later, forever —
    # roughly 1,000 billed queries on a single leg, outside the budget cap
    # and invisible to the counter. See carrier.py.
    ("carrier_tries",         "INTEGER NOT NULL DEFAULT 0"),
    ("carrier_tried_at",      "TEXT"),
    # How many LATE attempts have been made to fetch the airline's gate-in
    # AFTER the leg already closed on other evidence, and when the last one
    # was. Separate from api_queries_used because they answer different
    # questions: that counter is the leg's live ticket allowance, which is
    # spent and finished by the time these fire. Recorded BEFORE the call is
    # made, for the same reason carrier_tries is — a timeout still counts.
    # See enrichment.should_backfill_gate_in.
    ("gatein_tries",          "INTEGER NOT NULL DEFAULT 0"),
    ("gatein_tried_at",       "TEXT"),
    ("last_api_query_at",     "TEXT"),
    # Which account's AeroAPI key paid for the most recent query on this
    # shared row. Recorded so spend is attributable when several crew are
    # on the same flight.
    ("api_paid_by",           "INTEGER"),
    ("last_api_reason",       "TEXT"),
    ("api_raw",               "TEXT"),
    ("last_polled_at",        "TEXT"),
    ("created_at",            "TEXT"),
    # Set when the leg closes. One indexed delete does the 30-day cleanup.
    ("purge_after",           "TEXT"),
    # ---- test mode (1.6.0)
    # 1 on a leg invented by app/simulator.py. This is the ONLY thing that
    # separates a rehearsal from a real flight, so it is checked at every
    # boundary where the difference matters: AeroAPI must never be spent on
    # one, ADS-B must never be asked about one, and no export or logbook may
    # ever count one. Defaulting to 0 means every existing row is real,
    # which is the correct reading of a database written before test mode
    # existed.
    ("simulated",             "INTEGER NOT NULL DEFAULT 0"),
    # Which scenario produced it, for the panel to label the row.
    ("sim_scenario",          "TEXT"),
]


def _create_flights(conn) -> None:
    cols = ",\n                ".join(f"{n} {t}" for n, t in FLIGHT_COLUMNS)
    # id is the flight key: DATE-FLIGHTNUMBER-ORIGIN-DEST. Derived from the
    # flight itself, so two crew on the same leg produce the same id and
    # therefore share the row. There is no "-DH" suffix here — deadheading
    # describes a person, not an aeroplane, and lives on `roster`.
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS flights (
            id TEXT PRIMARY KEY,
            {cols}
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_flights_number_date ON flights(flight_number, date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_flights_purge ON flights(purge_after)")

    # Who is on which flight, and in what capacity.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS roster (
            user_id INTEGER NOT NULL,
            flight_id TEXT NOT NULL,
            sort_index INTEGER NOT NULL DEFAULT 0,
            is_deadhead INTEGER NOT NULL DEFAULT 0,
            trip_start INTEGER NOT NULL DEFAULT 0,
            added_at TEXT,
            PRIMARY KEY (user_id, flight_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_roster_user ON roster(user_id, sort_index)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_roster_flight ON roster(flight_id)")


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


def get_meta(key: str, default: str = "") -> str:
    """Read one installation-level value. Never raises on a missing table."""
    conn = get_connection()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS app_meta ("
                     "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        r = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else default
    finally:
        conn.close()


def set_meta(key: str, value: str) -> None:
    conn = get_connection()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS app_meta ("
                     "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
                     (key, str(value)))
        conn.commit()
    finally:
        conn.close()


def _stamp_schema_version(conn) -> None:
    """Record what shape this database is, and refuse to wreck a newer one.

    Migrations here have always been append-only and idempotent, which makes
    them safe to RUN repeatedly. What they could not do is answer "what is
    this database?" without inspecting every table, and they could not tell
    the difference between a database that predates a change and one that
    postdates it.

    Two things this buys, both of which only matter later:

      * ORDERING. Once migrations are numbered, a database that reports 3 can
        run 4, 5, 6 in sequence rather than every migration re-deciding for
        itself whether it already ran.

      * A GUARD AGAINST DOWNGRADES. Rolling back to an older image and
        pointing it at a newer database is the single easiest way to lose
        data: the old build does not know about the new columns, writes rows
        without them, and the damage is only visible later. A database
        stamped NEWER than the running build is a hard stop, not a warning,
        because by the time a warning is read the writes have happened.

    Deliberately NOT a hard stop in the other direction: an older database
    with a newer build is the normal upgrade path and must just work.
    """
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO app_meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),))
        return
    try:
        found = int(row["value"])
    except (TypeError, ValueError):
        found = SCHEMA_VERSION
    if found > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema is version {found} but this build only "
            f"understands {SCHEMA_VERSION}. This is an older build being "
            f"pointed at a newer database. Refusing to start rather than "
            f"writing rows that silently drop the newer columns. Deploy the "
            f"matching build, or restore a backup from before the upgrade."
        )
    if found < SCHEMA_VERSION:
        # Column-level migrations above already ran and are idempotent, so
        # reaching current is simply a matter of recording that they did.
        print(f"[db] schema {found} -> {SCHEMA_VERSION}")
        conn.execute(
            "UPDATE app_meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),))


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
                icon_style TEXT DEFAULT 'modern',
                poll_seconds INTEGER DEFAULT 45,
                show_flightaware INTEGER DEFAULT 1,
                show_fr24 INTEGER DEFAULT 1,
                is_admin INTEGER DEFAULT 0,
                aeroapi_enabled INTEGER NOT NULL DEFAULT 0,
                aeroapi_key TEXT,
                aeroapi_queries INTEGER NOT NULL DEFAULT 0,
                aeroapi_budget REAL NOT NULL DEFAULT 4.90,
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
            ("aeroapi_budget", "REAL NOT NULL DEFAULT 4.90"),
            ("aeroapi_reported_cost", "REAL"),
            ("aeroapi_reported_calls", "INTEGER"),
            ("aeroapi_usage_at", "TEXT"),
            ("aeroapi_period", "TEXT"),
            ("show_flightaware", "INTEGER DEFAULT 1"),
            ("show_fr24", "INTEGER DEFAULT 1"),
            # v7.5. Which plane silhouette this user sees, on the map AND as
            # the installed app icon. Defaults to the shape every install had
            # before the setting existed, so an upgrade changes nothing until
            # somebody chooses otherwise.
            ("icon_style", "TEXT DEFAULT 'modern'"),
        ]:
            if name not in ucols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")
        if "is_admin" not in ucols:
            # Whoever already exists (the original single pilot) becomes
            # admin automatically on upgrade.
            conn.execute("UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users)")

        # v5.2 raised the default cap from $4.50 to $4.90. Changing the
        # column DEFAULT only affects rows created afterwards, so anyone
        # already installed would keep the old ceiling and quietly lose
        # 40 cents of the free allowance. Only rows sitting on EXACTLY the
        # old default move — a pilot who chose 4.50, or any other number,
        # picked it, and this must not overwrite a deliberate choice.
        conn.execute("UPDATE users SET aeroapi_budget = 4.90 WHERE aeroapi_budget = 4.50")

        # A v5.0 database has a per-user `flights` table with a composite
        # primary key. The shared table cannot be created over the top of
        # it (CREATE TABLE IF NOT EXISTS would silently do nothing and
        # every write would then hit the wrong shape), so it is renamed
        # out of the way first and merged in below.
        if _table_exists(conn, "flights"):
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(flights)")}
            if "user_id" in cols:
                conn.execute("ALTER TABLE flights RENAME TO flights_v50")
                print("[db] found a per-user v5.0 flights table — merging into shared")

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
        # Small key/value store for things that belong to the installation
        # rather than to any one pilot — currently just the session-signing
        # key. It lives in the database because the database is the one
        # thing that reliably survives a redeploy; a key kept anywhere else
        # going missing logs everybody out.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS debug_events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                at      TEXT NOT NULL,
                event   TEXT NOT NULL,
                subject TEXT,
                detail  TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_debug_at ON debug_events(id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_debug_subject "
                     "ON debug_events(subject, id DESC)")
        _stamp_schema_version(conn)
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
        # v5.2 taught the parser to drop FFDO placeholder lines (same
        # airport both ends, or flight number zero). That only helps
        # FUTURE imports — anything already saved stayed saved, kept
        # showing on the tracker and the calendar, and kept being swept by
        # the poller. Clean them out once, here, rather than asking the
        # pilot to delete them by hand from the Flights page.
        placeholder = ("SELECT id FROM flights WHERE origin = destination "
                       "OR TRIM(COALESCE(flight_number,'')) IN ('','0','00','000','0000')")
        stale = [r["id"] for r in conn.execute(placeholder)]
        if stale:
            marks = ",".join("?" * len(stale))
            conn.execute(f"DELETE FROM roster WHERE flight_id IN ({marks})", stale)
            conn.execute(f"DELETE FROM positions WHERE flight_key IN ({marks})", stale)
            conn.execute(f"DELETE FROM flights WHERE id IN ({marks})", stale)
            print(f"[db] removed {len(stale)} FFDO placeholder rows "
                  f"(same airport both ends, or flight number zero)")

        conn.commit()
    finally:
        conn.close()


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _migrate_from_v4(conn) -> None:
    """Carry the schedule and the flown tracks over from older schemas.

    Deliberately partial. The schedule is irreplaceable — it was typed in —
    and tracks are irreplaceable, they were observed. The v4 airline
    enrichment and closeout blobs are neither: they are at most 30 days
    old, they can be re-fetched, and mapping two nested JSON documents into
    eighty columns is a one-off guess. Past flights keep their route and
    their path; their gate times start over.

    Handles both v4 (seven tables) and v5.0 (per-user `flights`).
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

    have_roster = conn.execute("SELECT COUNT(*) c FROM roster").fetchone()["c"]
    if have_roster:
        return

    # --- from v5.0: `flights` was per-user and carried the roster fields.
    if _table_exists(conn, "flights_v50"):
        rows = conn.execute("SELECT * FROM flights_v50").fetchall()
        _adopt(conn, rows, id_key="id", user_key="user_id")
        # v5.0 already stored observed and airline data in the same column
        # shape, so unlike the v4 path there is nothing to guess — copy it
        # across. Where two crew had rows for one flight, the richer one
        # wins field by field, since a blank is only ever absence.
        carried = {n for n, _ in FLIGHT_COLUMNS} & {
            r["name"] for r in conn.execute("PRAGMA table_info(flights_v50)")}
        carried -= {"date", "flight_number", "origin", "destination",
                    "dep_time_local", "arr_time_local", "created_at"}
        for col in sorted(carried):
            conn.execute(
                f"UPDATE flights SET {col} = COALESCE("
                f"  (SELECT v.{col} FROM flights_v50 v "
                f"   WHERE (CASE WHEN v.id LIKE '%-DH' "
                f"          THEN substr(v.id, 1, length(v.id) - 3) ELSE v.id END) = flights.id "
                f"   AND v.{col} IS NOT NULL LIMIT 1), {col})")
        print(f"[db] merged {len(rows)} per-user v5.0 rows into shared flights")
        return

    # --- from v4: the `legs` table.
    if _table_exists(conn, "legs"):
        old_cols = {r["name"] for r in conn.execute("PRAGMA table_info(legs)")}
        rows = conn.execute("SELECT * FROM legs").fetchall()
        _adopt(conn, rows, id_key="id", user_key="user_id",
               has_trip="trip_start" in old_cols,
               has_op="operator_callsign" in old_cols)
        n = conn.execute("SELECT COUNT(*) c FROM flights").fetchone()["c"]
        if n:
            print(f"[db] carried {n} schedule legs over from v4 "
                  f"({len(rows)} roster entries)")


def _strip_dh(leg_id: str) -> str:
    """Old ids carried a "-DH" suffix. That describes the PERSON's role,
    not the aeroplane, so it is not part of a shared flight's identity."""
    return leg_id[:-3] if leg_id.endswith("-DH") else leg_id


def _adopt(conn, rows, id_key: str, user_key: str,
           has_trip: bool = True, has_op: bool = True) -> None:
    """Fold old per-user rows into one shared flight plus roster entries.

    Where two pilots had the same leg, the flight row is written once (the
    first wins; they described the same aeroplane) and each gets a roster
    entry carrying their own sort order and deadhead flag.
    """
    now = _now()
    for r in rows:
        try:
            raw_id = r[id_key]
            fid = _strip_dh(raw_id)
            uid = r[user_key] or 0
            is_dh = int(bool(r["is_deadhead"])) if "is_deadhead" in r.keys() else \
                (1 if raw_id.endswith("-DH") else 0)
            conn.execute(
                "INSERT OR IGNORE INTO flights (id, date, flight_number, origin, "
                "destination, dep_time_local, arr_time_local, operator_callsign, "
                "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (fid, r["date"], r["flight_number"], r["origin"], r["destination"],
                 r["dep_time_local"], r["arr_time_local"],
                 (r["operator_callsign"] if has_op else None), now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO roster (user_id, flight_id, sort_index, "
                "is_deadhead, trip_start, added_at) VALUES (?,?,?,?,?,?)",
                (uid, fid, r["sort_index"] or 0, is_dh,
                 int(bool(r["trip_start"])) if has_trip else 0, now),
            )
        except Exception:
            continue


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
