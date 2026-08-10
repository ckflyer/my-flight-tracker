"""The flight row: one place that owns every fact about a leg.

Three write modes, and choosing the right one is most of the correctness
of this app:

  ONCE    The first value we ever get is kept forever. Use for things that
          happened at a moment in time — wheels-off, the aircraft hex,
          the airline's originally published schedule. Writing these
          "latest wins" would let a re-query overwrite the truth with a
          later restatement of it.

  LATEST  The new value wins, BUT A BLANK NEVER OVERWRITES A KNOWN VALUE.
          Use for things that genuinely change — position, the airline's
          revised estimate, gate assignment. The blank guard is the whole
          point: a poll that comes back empty because the aircraft is over
          west Texas with no receiver nearby must not erase what we knew a
          minute ago. Losing the signal is a fact about our reception, not
          about the aeroplane.

  ALWAYS  Unconditional overwrite, including with NULL. Use only for
          recomputed derived values (progress, ETE) where "we can't work
          this out right now" is itself the correct thing to display.

`write_all_owners` exists because some facts belong to the AEROPLANE
rather than to a person. If two crew are on ENY3729, there is one
aircraft, one hex, one takeoff. Their rows are separate (each pilot's own
AeroAPI key pays for their own airline data, which matters on a
personal-use tier) but the observed facts are written to both.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .airports import enrich_leg
from .db import get_connection
from .models import FlightLeg


def flight_key(leg_id: str) -> str:
    """Shared identity of a physical flight.

    Leg ids look like "2026-08-04-3729-DFW-OKC", with "-DH" appended when
    the pilot is deadheading. That suffix describes the PERSON's role, not
    the aeroplane, so it's stripped here — otherwise a deadhead and a
    working leg on the same flight would record two half-tracks.
    """
    return leg_id[:-3] if leg_id.endswith("-DH") else leg_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ read
def get_row(user_id: int, leg_id: str):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM flights WHERE user_id = ? AND id = ?", (user_id, leg_id)
        ).fetchone()
    finally:
        conn.close()


def get_row_any(leg_id: str):
    """This leg as seen by whichever account has the most complete picture.

    Used where the question is about the AEROPLANE — has it flown, has it
    been closed out — which is not a per-account fact. Prefers a row that
    has airline data, since a pilot with a key knows more.
    """
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM flights WHERE id = ? "
            "ORDER BY (in_actual_api IS NOT NULL) DESC, "
            "         (last_api_query_at IS NOT NULL) DESC, user_id ASC LIMIT 1",
            (leg_id,),
        ).fetchone()
    finally:
        conn.close()


def owners_of(leg_id: str) -> List[int]:
    conn = get_connection()
    try:
        return [r["user_id"] for r in conn.execute(
            "SELECT DISTINCT user_id FROM flights WHERE id = ?", (leg_id,))]
    finally:
        conn.close()


def row_to_leg(row) -> Optional[FlightLeg]:
    if row is None:
        return None
    try:
        leg = FlightLeg(
            id=row["id"],
            date=date.fromisoformat(row["date"]),
            flight_number=row["flight_number"],
            origin=row["origin"],
            destination=row["destination"],
            dep_time_local=datetime.strptime(row["dep_time_local"], "%H:%M:%S").time(),
            arr_time_local=datetime.strptime(row["arr_time_local"], "%H:%M:%S").time(),
            is_deadhead=bool(row["is_deadhead"]),
            trip_start=bool(row["trip_start"]),
            operator_callsign=row["operator_callsign"],
        )
        enrich_leg(leg)
        return leg
    except Exception:
        return None


def legs_sharing_callsign(flight_number: str, on_date: date) -> List[FlightLeg]:
    """Every leg on this date flown under this flight number.

    Deliberately NOT scoped to one user: "which leg is this aeroplane
    flying right now" is a fact about the aeroplane. Deduplicated by
    flight key so the same leg on two pilots' schedules counts once.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM flights WHERE flight_number = ? AND date = ?",
            (flight_number, on_date.isoformat()),
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()

    seen, legs = set(), []
    for row in rows:
        leg = row_to_leg(row)
        if not leg:
            continue
        key = flight_key(leg.id)
        if key in seen:
            continue
        seen.add(key)
        legs.append(leg)
    legs.sort(key=lambda l: l.dep_datetime_utc()
              or datetime.min.replace(tzinfo=timezone.utc))
    return legs


# ----------------------------------------------------------------- write
def write(user_id: int, leg_id: str,
          once: Optional[Dict[str, Any]] = None,
          latest: Optional[Dict[str, Any]] = None,
          always: Optional[Dict[str, Any]] = None) -> None:
    """Apply the three merge modes in one statement.

    One UPDATE rather than three means a poll can't half-apply, and the
    row can never be seen mid-write by a page render.
    """
    sets, params = [], []
    for col, val in (once or {}).items():
        # Only fills a column that is still empty.
        sets.append(f"{col} = COALESCE({col}, ?)")
        params.append(val)
    for col, val in (latest or {}).items():
        # New value wins unless it is None. THE BLANK GUARD.
        sets.append(f"{col} = COALESCE(?, {col})")
        params.append(val)
    for col, val in (always or {}).items():
        sets.append(f"{col} = ?")
        params.append(val)
    if not sets:
        return
    params.extend([user_id, leg_id])
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE flights SET {', '.join(sets)} WHERE user_id = ? AND id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()


def write_all_owners(leg_id: str,
                     once: Optional[Dict[str, Any]] = None,
                     latest: Optional[Dict[str, Any]] = None,
                     always: Optional[Dict[str, Any]] = None) -> None:
    """Write a fact about the AEROPLANE to every account holding this leg."""
    for uid in owners_of(leg_id):
        write(uid, leg_id, once=once, latest=latest, always=always)


def save_schedule(user_id: int, legs: List[FlightLeg]) -> None:
    """Replace this user's schedule, keeping observed data where the same
    leg survives the re-import.

    Re-pasting a bid line is routine — a trip gets added, a leg moves — and
    the old code deleted every row first, which threw away the aircraft
    lock and every observed time for legs that hadn't changed. Now a leg
    that's still on the schedule keeps everything it had learned.
    """
    conn = get_connection()
    try:
        keep = {l.id for l in legs}
        existing = {r["id"] for r in conn.execute(
            "SELECT id FROM flights WHERE user_id = ?", (user_id,))}
        gone = existing - keep
        for leg_id in gone:
            conn.execute("DELETE FROM flights WHERE user_id = ? AND id = ?",
                         (user_id, leg_id))
        now = _now_iso()
        for idx, leg in enumerate(legs):
            if leg.id in existing:
                conn.execute(
                    "UPDATE flights SET sort_index = ?, date = ?, flight_number = ?, "
                    "origin = ?, destination = ?, dep_time_local = ?, arr_time_local = ?, "
                    "is_deadhead = ?, trip_start = ? WHERE user_id = ? AND id = ?",
                    (idx, leg.date.isoformat(), leg.flight_number, leg.origin,
                     leg.destination, leg.dep_time_local.isoformat(),
                     leg.arr_time_local.isoformat(), int(leg.is_deadhead),
                     int(leg.trip_start), user_id, leg.id),
                )
            else:
                conn.execute(
                    "INSERT INTO flights (id, user_id, sort_index, date, flight_number, "
                    "origin, destination, dep_time_local, arr_time_local, is_deadhead, "
                    "trip_start, operator_callsign, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (leg.id, user_id, idx, leg.date.isoformat(), leg.flight_number,
                     leg.origin, leg.destination, leg.dep_time_local.isoformat(),
                     leg.arr_time_local.isoformat(), int(leg.is_deadhead),
                     int(leg.trip_start), leg.operator_callsign, now),
                )
        conn.commit()
    finally:
        conn.close()


def load_schedule(user_id: int) -> List[FlightLeg]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM flights WHERE user_id = ? ORDER BY sort_index ASC",
            (user_id,),
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    return [leg for leg in (row_to_leg(r) for r in rows) if leg]


def delete_leg(user_id: int, leg_id: str) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM flights WHERE user_id = ? AND id = ?",
                     (user_id, leg_id))
        conn.commit()
    finally:
        conn.close()


def set_operator_callsign(leg_id: str, callsign: str) -> None:
    """Which carrier operates this leg. A fact about the flight, so it's
    written for every account holding it, and only once."""
    write_all_owners(leg_id, once={"operator_callsign": callsign})


# --------------------------------------------------------------- cleanup
RETENTION_DAYS = 30


def purge_old(now: Optional[datetime] = None) -> int:
    """Drop flights past their retention date, and any orphaned track.

    Rows get a purge_after stamp when they close. A leg that never closed
    is caught by its scheduled date instead, so nothing can linger forever
    just because it never resolved.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=RETENTION_DAYS)).date().isoformat()
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM flights WHERE (purge_after IS NOT NULL AND purge_after < ?) "
            "OR (purge_after IS NULL AND date < ?)",
            (now.isoformat(), cutoff),
        )
        removed = cur.rowcount or 0
        # A track with no flight left to draw it is dead weight.
        conn.execute(
            "DELETE FROM positions WHERE flight_key NOT IN "
            "(SELECT DISTINCT CASE WHEN id LIKE '%-DH' "
            " THEN substr(id, 1, length(id) - 3) ELSE id END FROM flights)"
        )
        conn.commit()
        return removed
    finally:
        conn.close()
