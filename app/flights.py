"""The flight row: one shared record per real-world flight.

SHARED. Not per-user. When a captain and an FO are both running this app on
the same leg, that is one aeroplane, one takeoff, one gate-in — so it is
one row, and one AeroAPI query serves both. `roster` records whose schedule
it is on and in what capacity.

  flights.id  = DATE-FLIGHTNUMBER-ORIGIN-DEST, derived from the flight, so
                two crew importing the same leg land on the same row.
  roster      = (user_id, flight_id) plus sort_index, is_deadhead,
                trip_start. Everything true of a PERSON rather than an
                aeroplane. Deadheading is the clearest case: the same
                flight is a working leg for one pilot and a deadhead for
                another.

THREE WRITE MODES, and choosing the right one is most of the correctness
of this app:

  ONCE    The first value we ever get is kept forever. For things that
          happened at a moment in time — wheels-off, the aircraft hex, the
          airline's originally published schedule. "Latest wins" would let
          a re-query overwrite the truth with a later restatement of it.

  LATEST  The new value wins, BUT A BLANK NEVER OVERWRITES A KNOWN VALUE.
          For things that genuinely change — position, revised estimates,
          gate assignment. The blank guard is the whole point: a poll that
          comes back empty because the aircraft is over west Texas with no
          receiver nearby must not erase what we knew a minute ago. Losing
          the signal is a fact about our reception, not about the aeroplane.

  ALWAYS  Unconditional overwrite, including with NULL. Only for recomputed
          derived values (progress, ETE) where "we can't work this out
          right now" is itself the correct thing to display.
"""
from __future__ import annotations

import os

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .airports import enrich_leg
from .db import get_connection
from .models import FlightLeg


def flight_key(leg_id: str) -> str:
    """Canonical shared id. Tolerates a legacy "-DH" suffix."""
    return leg_id[:-3] if leg_id.endswith("-DH") else leg_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ read
def get_flight(leg_id: str):
    """The shared row. Takes no user_id — there is only one."""
    conn = get_connection()
    try:
        return conn.execute("SELECT * FROM flights WHERE id = ?",
                            (flight_key(leg_id),)).fetchone()
    finally:
        conn.close()


def owners_of(leg_id: str) -> List[int]:
    """Every account with this flight on their schedule, lowest id first."""
    conn = get_connection()
    try:
        return [r["user_id"] for r in conn.execute(
            "SELECT user_id FROM roster WHERE flight_id = ? ORDER BY user_id",
            (flight_key(leg_id),))]
    finally:
        conn.close()


def crew_count(leg_id: str) -> int:
    return len(owners_of(leg_id))


def row_to_leg(row, is_deadhead: bool = False, trip_start: bool = False
               ) -> Optional[FlightLeg]:
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
            is_deadhead=is_deadhead,
            trip_start=trip_start,
            operator_callsign=row["operator_callsign"],
        )
        enrich_leg(leg)
        return leg
    except Exception:
        return None


def legs_sharing_callsign(flight_number: str, on_date: date) -> List[FlightLeg]:
    """Every leg on this date flown under this flight number.

    Not scoped to one user: "which leg is this aeroplane flying right now"
    is a fact about the aeroplane. With shared rows there is no longer any
    duplication to filter out.
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
    legs = [leg for leg in (row_to_leg(r) for r in rows) if leg]
    legs.sort(key=lambda l: l.dep_datetime_utc()
              or datetime.min.replace(tzinfo=timezone.utc))
    return legs


def _rostered_rows(where: str, params: tuple) -> List[Any]:
    """Rows matching `where`, restricted to flights somebody actually has.

    The EXISTS clause matters: retention keeps un-rostered rows around for a
    year (invariant 14), and none of them are anyone's flight any more. The
    poller has always walked schedules rather than this table, and these
    sweeps keep that property.
    """
    conn = get_connection()
    try:
        return conn.execute(
            f"SELECT f.* FROM flights f WHERE {where} AND EXISTS "
            "(SELECT 1 FROM roster r WHERE r.flight_id = f.id)", params
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()


# How far back the closeout sweep looks. A leg that has not resolved in a
# week is not going to, and re-judging it every 20 seconds forever is a
# cost with no answer at the end of it. It still shows honestly as "Not
# tracked" past UNTRACKED_AFTER; it simply stops being asked about.
CLOSEOUT_SWEEP_DAYS = 7


def unclosed_rows(now: Optional[datetime] = None) -> List[Any]:
    """Flights that have NOT closed and are no longer being swept.

    THE BUG THIS EXISTS FOR. `poller.active_flights()` only ever returns
    the CURRENT leg and imminent upcoming ones, and `get_current_info`
    releases a leg once it is `CURRENT_GRACE` (3h) past its SCHEDULED
    arrival and not still airborne. Nothing looked at it again after that.

    But `closure.decide` was written as though a leg is judged forever:

      * the BACKSTOP matures 3h after the REVISED arrival, which on any
        late flight is later than 3h after the scheduled one — so on
        exactly the flights it exists for, the leg was already abandoned
        before its own backstop came due;
      * `relaunch` needs a later sweep to notice the aircraft flying
        again, and there were no later sweeps;
      * the LONG STOP route needs the 30-minute mark to be observed by a
        sweep, which a leg that blocked in near the end of its grace never
        got.

    So a leg could block in and stay open forever — reported as blocking
    in at 07:00 and still open at 11:30. The 1.4.0 long-stop fix was
    correct and could not help, because by then nothing was asking.

    Judging these costs NOTHING: every value closure needs is already on
    the row, so this is pure re-evaluation with no network call.
    """
    now = now or datetime.now(timezone.utc)
    lo = (now - timedelta(days=CLOSEOUT_SWEEP_DAYS)).date().isoformat()
    hi = (now + timedelta(days=1)).date().isoformat()
    return _rostered_rows("f.closed = 0 AND f.date >= ? AND f.date <= ?", (lo, hi))


def rows_awaiting_gate_in(now: Optional[datetime] = None,
                          max_tries: int = 3) -> List[Any]:
    """Closed legs whose airline gate-in never turned up.

    `closure.maybe_close` has always been able to UPGRADE a provisional
    close to the airline's own figure — and could never do it, because a
    closed leg stopped being polled and `should_query` refuses to spend on
    one. The upgrade was a door with a wall behind it.

    These are the legs on the other side of that wall: closed on our own
    evidence, still missing `in_actual_api`, and therefore permanently
    incomplete in a logbook unless somebody goes back and asks.
    """
    now = now or datetime.now(timezone.utc)
    lo = (now - timedelta(days=CLOSEOUT_SWEEP_DAYS)).date().isoformat()
    return _rostered_rows(
        "f.closed = 1 AND f.in_actual_api IS NULL AND f.cancelled = 0 "
        "AND f.simulated = 0 "          # never chase an invented flight
        "AND f.closed_by IN ('observed','backstop','relaunch') "
        "AND f.gatein_tries < ? AND f.date >= ?", (max_tries, lo))


# ----------------------------------------------------------------- write
def write(leg_id: str,
          once: Optional[Dict[str, Any]] = None,
          latest: Optional[Dict[str, Any]] = None,
          always: Optional[Dict[str, Any]] = None) -> None:
    """Apply the three merge modes in one statement.

    One UPDATE rather than three means a poll can't half-apply, and the row
    can never be seen mid-write by a page render.
    """
    sets, params = [], []
    for col, val in (once or {}).items():
        sets.append(f"{col} = COALESCE({col}, ?)")      # fills only if empty
        params.append(val)
    for col, val in (latest or {}).items():
        sets.append(f"{col} = COALESCE(?, {col})")      # THE BLANK GUARD
        params.append(val)
    for col, val in (always or {}).items():
        sets.append(f"{col} = ?")
        params.append(val)
    if not sets:
        return
    params.append(flight_key(leg_id))
    conn = get_connection()
    try:
        conn.execute(f"UPDATE flights SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
    finally:
        conn.close()


def _upsert_leg(conn, user_id: int, leg: FlightLeg, now_iso: str) -> None:
    """Create the shared flight if new, and put this user on it.

    A flight another pilot already imported is ADOPTED, not duplicated —
    this user joins the existing row and immediately sees whatever has
    already been observed or paid for.

    Schedule fields are written only when the flight is NEW. If another
    pilot already created it, their FFDO times stand: they describe the
    same aeroplane, and overwriting them on every import would let two bid
    lines fight over one row.
    """
    fid = flight_key(leg.id)
    conn.execute(
        "INSERT OR IGNORE INTO flights (id, date, flight_number, origin, "
        "destination, dep_time_local, arr_time_local, operator_callsign, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (fid, leg.date.isoformat(), leg.flight_number, leg.origin,
         leg.destination, leg.dep_time_local.isoformat(),
         leg.arr_time_local.isoformat(), leg.operator_callsign, now_iso),
    )
    conn.execute(
        "INSERT INTO roster (user_id, flight_id, sort_index, is_deadhead, "
        "trip_start, added_at) VALUES (?,?,0,?,?,?) "
        "ON CONFLICT(user_id, flight_id) DO UPDATE SET "
        "is_deadhead = excluded.is_deadhead, trip_start = excluded.trip_start",
        (user_id, fid, int(leg.is_deadhead), int(leg.trip_start), now_iso),
    )


def _resequence(conn, user_id: int) -> None:
    """Renumber this user's whole roster into departure order.

    `load_schedule` reads `ORDER BY sort_index`, and before 1.5.0 the index
    was simply the position of the leg in the paste. That was fine while an
    import REPLACED the roster, because the paste was the whole roster. It
    stops being fine the moment imports accumulate: pasting September would
    number its legs 0..40 on top of August's 0..40 and the two months would
    interleave into nonsense.

    Sorting here rather than in `load_schedule` keeps the stored order and
    the displayed order the same thing, so nothing downstream has to know.
    """
    rows = conn.execute(
        "SELECT r.flight_id, f.date, f.dep_time_local FROM roster r "
        "JOIN flights f ON f.id = r.flight_id WHERE r.user_id = ?", (user_id,)
    ).fetchall()
    # Sorted on the stored LOCAL date and time rather than a resolved
    # instant: this only has to be stable and chronological, and going
    # through timezones.py for every row on every import would be a lot of
    # work to reorder two legs that depart minutes apart in adjacent zones.
    ordered = sorted(rows, key=lambda r: (r["date"] or "", r["dep_time_local"] or ""))
    for idx, r in enumerate(ordered):
        conn.execute("UPDATE roster SET sort_index = ? WHERE user_id = ? "
                     "AND flight_id = ?", (idx, user_id, r["flight_id"]))


def merge_schedule(user_id: int, legs: List[FlightLeg]) -> None:
    """ADD these legs to the roster. Removes nothing. (N1, 1.5.0)

    This is what an import runs now. `replace_schedule` below is the old
    behaviour and is no longer on the import path — see its docstring for
    why that mattered.
    """
    conn = get_connection()
    try:
        now = _now_iso()
        for leg in legs:
            _upsert_leg(conn, user_id, leg, now)
        _resequence(conn, user_id)
        conn.commit()
    finally:
        conn.close()


def remove_legs(user_id: int, leg_ids) -> int:
    """Drop specific legs from one person's roster. Returns how many went.

    Targeted and explicit, because after 1.5.0 removal only ever happens
    because a human ticked something. The shared flight row survives while
    anyone else still has it.
    """
    ids = {flight_key(i) for i in leg_ids if i}
    if not ids:
        return 0
    conn = get_connection()
    try:
        removed = 0
        for fid in ids:
            cur = conn.execute("DELETE FROM roster WHERE user_id = ? AND "
                               "flight_id = ?", (user_id, fid))
            removed += cur.rowcount or 0
        _resequence(conn, user_id)
        conn.commit()
        return removed
    finally:
        conn.close()


def replace_schedule(user_id: int, legs: List[FlightLeg]) -> None:
    """Make the roster exactly this list, deleting anything else.

    NOT USED BY IMPORT ANY MORE, and the rename is the point — this was
    called `save_schedule`, the import called it, and so pasting September
    silently deleted August. Combined with the old 30-day retention that
    made the app a rolling window, which is exactly what a logbook cannot
    be. See NEXT UP / N1.

    Kept because it is still the honest primitive for "this list, and
    nothing else", and the test suites use it to set up a roster in one
    call. Anything user-facing should be using merge_schedule and
    remove_legs, so that a deletion is always something a person asked for.
    """
    conn = get_connection()
    try:
        keep = {flight_key(l.id) for l in legs}
        existing = {r["flight_id"] for r in conn.execute(
            "SELECT flight_id FROM roster WHERE user_id = ?", (user_id,))}
        for gone in existing - keep:
            conn.execute("DELETE FROM roster WHERE user_id = ? AND flight_id = ?",
                         (user_id, gone))
        now = _now_iso()
        for leg in legs:
            _upsert_leg(conn, user_id, leg, now)
        _resequence(conn, user_id)
        conn.commit()
    finally:
        conn.close()


def load_schedule(user_id: int) -> List[FlightLeg]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT f.*, r.is_deadhead AS r_dh, r.trip_start AS r_ts "
            "FROM roster r JOIN flights f ON f.id = r.flight_id "
            "WHERE r.user_id = ? ORDER BY r.sort_index ASC", (user_id,),
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        leg = row_to_leg(r, bool(r["r_dh"]), bool(r["r_ts"]))
        if leg:
            out.append(leg)
    return out


def delete_leg(user_id: int, leg_id: str) -> None:
    """Drop this flight from one person's schedule.

    The flight row itself survives while anyone else still has it — one
    pilot removing a leg must not delete a colleague's flight, or the
    tracking data everyone shares. Orphans are swept by purge_old.
    """
    conn = get_connection()
    try:
        conn.execute("DELETE FROM roster WHERE user_id = ? AND flight_id = ?",
                     (user_id, flight_key(leg_id)))
        conn.commit()
    finally:
        conn.close()


def set_operator_callsign(leg_id: str, callsign: str) -> None:
    write(leg_id, once={"operator_callsign": callsign})


# --------------------------------------------------------------- cleanup
# v7.5: was 30. A leg used to be deleted a month after it flew — track,
# gates, actual times, closeout, everything. That made the app a live view
# with a short memory rather than a record of what you fly.
#
# One year, not forever: a full year of tracks is a few tens of megabytes
# (breadcrumbs are thinned on write and capped per leg), so the storage
# argument for pruning is gone, but an unbounded position history of where
# crew members have been is a liability that grows on its own. A year is
# long enough to cover a bid cycle, a tax year, and any "when did we last
# fly together" question, and short enough to stay defensible in a privacy
# policy.
#
# Overridable so an installation can choose its own answer without a code
# change. 0 or negative disables purging entirely.
RETENTION_DAYS = int(os.environ.get("PT_RETENTION_DAYS") or 365)


def purge_old(now: Optional[datetime] = None) -> int:
    """Drop flights past retention, plus roster rows and tracks left behind.

    Rows get a purge_after stamp when they close. A leg that never closed
    is caught by its scheduled date instead, so nothing lingers forever
    just because it never resolved.

    RETENTION IS THE ONLY THING THAT DELETES A FLIGHT. Through v5.6 there
    was a second rule — "delete any flight nobody has on their schedule any
    more" — and it was wrong for the way this app is actually used. The
    flights table records real-world flights, shared by everyone who was on
    board; whether a given pilot still has the leg on his roster today says
    nothing about whether the flight happened. Two things that rule broke:

      * Swapping in a test schedule to watch live traffic un-rostered every
        real leg, and the next sweep (every 6 hours, and on the first tick
        after the container starts, so any update.sh triggered it) deleted
        them outright — gates, actual times, closeout, the lot. Re-pasting
        the real bid line then found no row to adopt and built a blank one
        from the schedule. The tracks went on the sweep after that.
      * An FO importing a bid line for a trip already flown could only
        adopt rows that still happened to be on someone's schedule.

    Now an unrostered flight simply ages out with everything else at
    RETENTION_DAYS. Nothing polls it in the meantime — the poller walks
    each user's schedule, not this table — so a retained row costs storage
    and no AeroAPI queries.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=RETENTION_DAYS)).date().isoformat()
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM flights WHERE (purge_after IS NOT NULL AND purge_after < ?) "
            "OR (purge_after IS NULL AND date < ?)", (now.isoformat(), cutoff))
        removed = cur.rowcount or 0
        # Cleanup runs AFTER the only deletion above, so a row and its
        # dependents go in the same sweep rather than one sweep apart.
        conn.execute("DELETE FROM roster WHERE flight_id NOT IN (SELECT id FROM flights)")
        conn.execute("DELETE FROM positions WHERE flight_key NOT IN (SELECT id FROM flights)")
        conn.commit()
        return removed
    finally:
        conn.close()
