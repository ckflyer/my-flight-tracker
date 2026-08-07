"""Closing a leg out, once and for good.

Closure used to be implicit: several places independently decided a flight
was over, none of them authoritative, and nothing was ever frozen — a
"finished" leg kept recomputing from whatever data drifted in later. This
module makes it one decision with one recorded reason.

POLICY
------
With an AeroAPI key, OOOI is the authority: the airline's own gate-in
(actual_in) closes the leg, or a cancellation. ADS-B still drives the map
and can advance the displayed status, but it cannot close.

Without a key, ADS-B does the best it can:
  * the aircraft flew, then came to a stop — the ground cycle, or
  * the aircraft went airborne AGAIN after landing, which is unambiguous
    and needs no timers at all.

BACKSTOP
--------
actual_in is the OOOI field most often missing, and there are documented
cases of it never publishing. A leg that never closes never releases its
callsign, which brings the same-callsign turn bug straight back. So a leg
observed stopped and well past its scheduled arrival closes anyway — but
records closed_by as "observed" rather than claiming airline authority. A
late actual_in can still upgrade it.

Once closed, the leg is FROZEN: no polling, no live data, no
recomputation. The stored summary records which source each figure came
from, so a past flight can say "arrived 4:11 (airline)" or "~4:11
(observed)" instead of presenting a guess as fact.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .db import get_connection

# An aircraft that has actually blocked in goes quiet — engines and
# transponder off. One holding off-gate waiting for a stand can sit
# stationary for half an hour and keep transmitting the whole time. So a
# stop only counts as arrival when the signal has ALSO gone away.
STOPPED_MIN = timedelta(minutes=5)
SIGNAL_GONE_MIN = timedelta(minutes=8)

# Last resort, and anchored to the REVISED arrival rather than the
# scheduled one — a flight running six hours late is still a live flight,
# and closing it on schedule alone would be wrong. This only fires when
# there is genuinely nothing else to go on.
BACKSTOP_AFTER_ARRIVAL = timedelta(hours=3)

# Sources, most authoritative first.
SOURCE_AIRLINE = "airline"        # AeroAPI actual_in
SOURCE_CANCELLED = "cancelled"
SOURCE_RELAUNCH = "observed_relaunch"   # aircraft flew again
SOURCE_OBSERVED = "observed"      # we saw it stop after flying
SOURCE_BACKSTOP = "backstop"      # well past arrival, never confirmed

# An airline gate-in may replace one of these after the fact.
UPGRADEABLE = {SOURCE_RELAUNCH, SOURCE_OBSERVED, SOURCE_BACKSTOP}


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def get_closeout(user_id: int, leg_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT closed_at, closed_by, summary FROM flight_closeout "
            "WHERE leg_id = ? AND user_id = ?",
            (leg_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        data = json.loads(row["summary"])
    except Exception:
        data = {}
    data["closed_at"] = row["closed_at"]
    data["closed_by"] = row["closed_by"]
    return data


def any_closeout(leg_id: str) -> bool:
    """Has this leg been closed by ANY account?

    Closure is a fact about the flight, not about one pilot's view of it,
    so a leg closed on one account is closed for tracking purposes on all.
    """
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT 1 FROM flight_closeout WHERE leg_id = ? LIMIT 1", (leg_id,)
        ).fetchone() is not None
    finally:
        conn.close()


def is_closed(user_id: int, leg_id: str) -> bool:
    return get_closeout(user_id, leg_id) is not None


def _store(user_id: int, leg_id: str, closed_at: datetime, closed_by: str,
           summary: Dict[str, Any]) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO flight_closeout "
            "(leg_id, user_id, closed_at, closed_by, summary) VALUES (?, ?, ?, ?, ?)",
            (leg_id, user_id, closed_at.isoformat(), closed_by, json.dumps(summary)),
        )
        conn.commit()
    finally:
        conn.close()


def reference_arrival(leg, enr: Optional[Dict[str, Any]]) -> Optional[datetime]:
    """The best current estimate of when this flight actually gets in.

    Anchoring the backstop to the SCHEDULED arrival would close a flight
    that's simply running very late — six hours late is a real and normal
    thing. The revised figure moves with the delay, so the backstop stays
    three hours behind reality instead of three hours behind the plan.
    """
    enr = enr or {}
    for key in ("actual_in", "estimated_in", "actual_on", "estimated_on"):
        parsed = _parse(enr.get(key))
        if parsed:
            return parsed
    return leg.arr_datetime_utc()


def decide(leg, enr: Optional[Dict[str, Any]], now: datetime,
           has_api: bool, observed_in: Optional[datetime],
           relaunched: bool, stopped_for: Optional[float] = None,
           signal_gap: Optional[float] = None,
           enrichment_fresh: bool = False) -> Optional[tuple]:
    """Should this leg close, and on whose authority?

    Returns (closed_by, closed_at) or None.
    """
    if enr:
        if enr.get("cancelled"):
            return (SOURCE_CANCELLED, now)
        actual_in = _parse(enr.get("actual_in"))
        if actual_in:
            return (SOURCE_AIRLINE, actual_in)

    # The aircraft is flying again — whatever the airline has published,
    # this leg is finished.
    if relaunched:
        return (SOURCE_RELAUNCH, now)

    # An observed arrival needs BOTH: stationary long enough, and the
    # signal gone. Either alone is ambiguous — a stop on its own is just
    # as likely to be waiting for a gate.
    blocked_in = (
        observed_in is not None
        and stopped_for is not None and stopped_for >= STOPPED_MIN.total_seconds()
        and signal_gap is not None and signal_gap >= SIGNAL_GONE_MIN.total_seconds()
    )
    if not has_api and blocked_in:
        return (SOURCE_OBSERVED, observed_in)

    # Backstop. Deliberately hard to reach: it only fires when there is
    # nothing else left to learn — no live signal, no fresh airline data —
    # and only well past the REVISED arrival, so a six-hour delay doesn't
    # close a flight that's still going.
    ref = reference_arrival(leg, enr)
    quiet = signal_gap is None or signal_gap >= SIGNAL_GONE_MIN.total_seconds()
    if (ref and now >= ref + BACKSTOP_AFTER_ARRIVAL
            and quiet and not enrichment_fresh):
        return (SOURCE_BACKSTOP, observed_in or now)
    return None


def build_summary(leg, enr: Optional[Dict[str, Any]], closed_by: str,
                  observed_in: Optional[datetime]) -> Dict[str, Any]:
    """Freeze what happened, and where each figure came from."""
    enr = enr or {}
    arrival_source = (
        "airline" if enr.get("actual_in")
        else "observed" if observed_in
        else "estimated" if enr.get("estimated_in")
        else None
    )
    arrival = (enr.get("actual_in")
               or (observed_in.isoformat() if observed_in else None)
               or enr.get("estimated_in"))
    return {
        "flight": leg.callsign,
        "origin": leg.origin,
        "destination": enr.get("destination") or leg.destination,
        "diverted": bool(enr.get("diverted")),
        "cancelled": bool(enr.get("cancelled")),
        "out": enr.get("actual_out"),
        "off": enr.get("actual_off"),
        "on": enr.get("actual_on"),
        "in": arrival,
        "arrival_source": arrival_source,
        "departure_source": "airline" if enr.get("actual_out") else None,
        "gate_origin": enr.get("gate_origin"),
        "gate_destination": enr.get("gate_destination"),
        "terminal_origin": enr.get("terminal_origin"),
        "terminal_destination": enr.get("terminal_destination"),
        "registration": enr.get("registration"),
        "scheduled_out": leg.dep_datetime_utc().isoformat() if leg.dep_datetime_utc() else None,
        "scheduled_in": leg.arr_datetime_utc().isoformat() if leg.arr_datetime_utc() else None,
        "closed_by": closed_by,
    }


def maybe_close(user_id: int, leg, enr, now: datetime, has_api: bool,
                observed_in: Optional[datetime], relaunched: bool,
                stopped_for: Optional[float] = None,
                signal_gap: Optional[float] = None,
                enrichment_fresh: bool = False) -> Optional[str]:
    """Close the leg if it's time. Returns the source, or None.

    An already-closed leg is left alone, EXCEPT that a late airline gate-in
    may upgrade a provisional close — the airline's own number is worth
    having even if it turns up an hour after we gave up waiting.
    """
    existing = get_closeout(user_id, leg.id)
    if existing:
        if (existing.get("closed_by") in UPGRADEABLE
                and enr and enr.get("actual_in")):
            closed_at = _parse(enr["actual_in"]) or now
            summary = build_summary(leg, enr, SOURCE_AIRLINE, observed_in)
            _store(user_id, leg.id, closed_at, SOURCE_AIRLINE, summary)
            print(f"[closure] {leg.id}: upgraded to airline gate-in")
            return SOURCE_AIRLINE
        return None

    verdict = decide(leg, enr, now, has_api, observed_in, relaunched,
                     stopped_for=stopped_for, signal_gap=signal_gap,
                     enrichment_fresh=enrichment_fresh)
    if not verdict:
        return None
    closed_by, closed_at = verdict
    _store(user_id, leg.id, closed_at, closed_by,
           build_summary(leg, enr, closed_by, observed_in))
    print(f"[closure] {leg.id}: closed by {closed_by} at {closed_at.isoformat()}")
    return closed_by
