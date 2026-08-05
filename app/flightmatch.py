"""Which physical aircraft is flying this leg?

Live data is looked up by callsign, and a callsign is not unique to a leg.
Regional turns fly out and back under one flight number — 3700 DFW-MFE and
3700 MFE-DFW the same day — and the return departs well inside the 3-hour
window the outbound leg stays "current" for. A plain callsign match locks
onto the wrong aircraft, and with the background poller running it writes
an entire return flight into the outbound leg's stored track.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not judge an aircraft by which way it is pointing. An earlier
version rejected aircraft heading away from the destination, which breaks
the moment a flight diverts: a DFW-OKC leg diverting back to DFW looks
exactly like the return flight, so the guard would disown the aircraft at
precisely the moment anyone watching most needs to see it. Holding
patterns, departures on the opposite flow and wide arrival vectoring have
the same problem. Geometry cannot tell "going somewhere else" apart from
"going somewhere else on purpose".

There is no route field to compare against either: ADS-B does not
transmit origin or destination — it is data straight off the aircraft —
and airplanes.live exposes no route endpoint. Origin/destination would
require fusing a separate commercial source.

WHAT IT DOES INSTEAD
--------------------
Identity. Every aircraft broadcasts a unique ICAO 24-bit hex address.

  1. ACQUIRE — a leg adopts an aircraft bearing its callsign either when
     that aircraft is at the leg's ORIGIN, or during a window around
     scheduled departure (for outstations with no ADS-B coverage, where a
     flight may only appear in the feed once it's already enroute). Its
     hex is recorded in flight_aircraft. The return leg of a turn can't
     depart until this one has landed and turned around, so it is never
     within that window.
  2. HOLD — from then on only that hex is accepted, and it is accepted
     unconditionally, wherever it goes. Diversions, holds, weather
     deviations and returns to the departure field are all followed.
  3. RELEASE — once the leg reads Arrived, tracking stops. Anything on the
     callsign after that belongs to the next flight.

The only positional test happens at acquisition, and it can never drop an
aircraft mid-flight.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .db import get_connection
from .geo import haversine_nm
from .models import FlightLeg
from .schedule import legs_sharing_callsign

# How close to the origin an aircraft must be before a leg adopts it.
# Generous enough to cover pushback, taxi, takeoff and initial climb.
ACQUIRE_RADIUS_NM = float(os.environ.get("ACQUIRE_RADIUS_NM", "30"))

# Requiring an aircraft to be SEEN at the origin is precise but brittle:
# plenty of outstations have no ADS-B receiver nearby, so a flight can
# depart, climb out and be halfway to the destination before it ever
# appears in the feed. With acquisition tied strictly to the origin, that
# leg would never be tracked at all.
#
# So there's a second way in: shortly after scheduled departure, adopt the
# aircraft on this callsign wherever it happens to be. This stays safe
# against the turn problem for timing reasons — the return leg can't
# depart until this one has landed and turned around, which is at minimum
# block time plus a turn, always well beyond this window.
ACQUIRE_AFTER_DEP_MINUTES = float(os.environ.get("ACQUIRE_AFTER_DEP_MINUTES", "45"))
ACQUIRE_BEFORE_DEP_MINUTES = float(os.environ.get("ACQUIRE_BEFORE_DEP_MINUTES", "20"))

# A leg is one ground -> airborne -> ground cycle. Once the acquired
# aircraft has flown and then come to a complete stop, the flight is over —
# wherever it stopped. That last part matters: a diversion ends the leg at
# the alternate, which is correct, and it means completion never depends on
# reaching the scheduled destination.
GROUND_STOP_KTS = 5.0            # below this it's parked, not taxiing
GROUND_COMPLETE_SECONDS = float(os.environ.get("GROUND_COMPLETE_SECONDS", "300"))


def flight_key_for(leg: FlightLeg) -> str:
    from .track import flight_key
    return flight_key(leg.id)


def active_sibling(leg: FlightLeg, now: datetime) -> Optional[FlightLeg]:
    """Of the legs sharing this callsign today, which one is flying NOW?

    A regional turn reuses one flight number in both directions, so
    ENY3700 can mean DFW-MFE this morning and MFE-DFW at midday. Asking
    "is this aircraft mine?" leg-by-leg cannot answer that — both legs see
    the same callsign and the same airframe.

    The schedule can, and deterministically: the leg in progress is the
    LATEST one whose scheduled departure has arrived (allowing the usual
    pre-departure lead-in). No timers, no thresholds, no inference from
    position — which matters because the ground-cycle check needs to see
    the aircraft stopped at the outstation, and small fields often have no
    ADS-B coverage at all, so that check frequently never fires.
    """
    siblings = legs_sharing_callsign(leg.flight_number, leg.date)
    if len(siblings) < 2:
        return None   # nothing to arbitrate

    cutoff = now + timedelta(minutes=ACQUIRE_BEFORE_DEP_MINUTES)
    started = [l for l in siblings if (l.dep_datetime_utc() or now) <= cutoff]
    if not started:
        # Before any of them departs, the first one is next up.
        return siblings[0]
    return max(started, key=lambda l: l.dep_datetime_utc() or now)


def _enrichment_for(leg: FlightLeg) -> Optional[Dict[str, Any]]:
    """Cached AeroAPI record for this leg, from any account that has it.

    Read-only and free — enrichment is fetched by the poller on a budget,
    never here.
    """
    from .enrichment import get_enrichment
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT user_id FROM flight_enrichment WHERE leg_id = ? ORDER BY fetched_at DESC LIMIT 1",
            (leg.id,),
        ).fetchall()
    except Exception:
        return None
    finally:
        conn.close()
    if not rows:
        return None
    return get_enrichment(rows[0]["user_id"], leg.id)


def _row(leg: FlightLeg):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM flight_aircraft WHERE flight_key = ?", (flight_key_for(leg),)
        ).fetchone()
    finally:
        conn.close()


def observed_gate_in(leg: FlightLeg) -> Optional[datetime]:
    """When WE saw the aircraft come to a stop after flying.

    This is the closest thing to the parking-brake moment that ADS-B can
    give, and it beats a stale estimated_in: AeroAPI publishes actual_in
    with a lag, so a flight can be sitting at the gate while the only
    airline figure available is still a forecast from an hour ago. Showing
    that forecast as though it were the arrival time is how "arrived 4:05,
    11 minutes early" appeared for a flight that actually blocked in at
    4:11.
    """
    row = _row(leg)
    if not row or not row["airborne_seen"] or not row["stopped_since"]:
        return None
    try:
        return datetime.fromisoformat(row["stopped_since"])
    except Exception:
        return None


def is_complete(leg: FlightLeg) -> bool:
    row = _row(leg)
    return bool(row and row["completed_at"])


def observe(leg: FlightLeg, state: Dict[str, Any], now: datetime) -> bool:
    """Fold one observation into the leg's flight-cycle state.

    Returns True if this observation completes the leg.

    Two signals, and they only mean anything together:

      * GROUND CYCLE — the aircraft has flown, and has now been stopped on
        the ground for GROUND_COMPLETE_SECONDS. Stopped, not merely on the
        ground, so a long taxi-in doesn't end the leg early and lose the
        taxi track.
      * SQUAWK — a new transponder code seen while stopped on the ground
        means a new flight plan is active, so the previous leg is
        definitely over. This is ONLY consulted on the ground. Codes are
        routinely reassigned in flight when an aircraft is handed from one
        ATC facility to the next, so an airborne squawk change carries no
        information about whether the flight has ended and is ignored.
    """
    row = _row(leg)
    if not row:
        return False

    on_ground = bool(state.get("on_ground"))
    speed = state.get("speed_kts")
    squawk = state.get("squawk")
    airborne_seen = bool(row["airborne_seen"])
    stopped_since = row["stopped_since"]
    last_squawk = row["last_squawk"]

    completed = False
    new_stopped_since = stopped_since

    if not on_ground:
        airborne_seen = True
        new_stopped_since = None          # airborne: no ground timer running
    else:
        stopped = speed is not None and speed <= GROUND_STOP_KTS
        if stopped:
            if new_stopped_since is None:
                new_stopped_since = now.isoformat()
            elif airborne_seen:
                try:
                    elapsed = (now - datetime.fromisoformat(new_stopped_since)).total_seconds()
                except Exception:
                    elapsed = 0
                if elapsed >= GROUND_COMPLETE_SECONDS:
                    completed = True
            # New code while parked = a new flight plan = this leg is done.
            # Never evaluated in the air.
            if (airborne_seen and squawk and last_squawk and squawk != last_squawk):
                completed = True
        else:
            new_stopped_since = None      # taxiing, not finished

    conn = get_connection()
    try:
        conn.execute(
            "UPDATE flight_aircraft SET airborne_seen = ?, stopped_since = ?, "
            "last_squawk = COALESCE(?, last_squawk), completed_at = ? WHERE flight_key = ?",
            (
                int(airborne_seen),
                new_stopped_since,
                # Only ever remember a squawk observed on the ground, so an
                # in-flight reassignment can't poison the comparison.
                squawk if on_ground else None,
                now.isoformat() if completed else row["completed_at"],
                flight_key_for(leg),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return completed


def get_acquired_aircraft(leg: FlightLeg) -> Optional[str]:
    """The hex already locked in for this leg, if any."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT icao24 FROM flight_aircraft WHERE flight_key = ?",
            (flight_key_for(leg),),
        ).fetchone()
        return row["icao24"] if row else None
    finally:
        conn.close()


def acquire_aircraft(leg: FlightLeg, icao24: str, now: datetime) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO flight_aircraft (flight_key, icao24, acquired_at) "
            "VALUES (?, ?, ?)",
            (flight_key_for(leg), icao24, now.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def release_aircraft(leg: FlightLeg) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM flight_aircraft WHERE flight_key = ?",
                     (flight_key_for(leg),))
        conn.commit()
    finally:
        conn.close()


class Verdict:
    def __init__(self, accepted: bool, reason: Optional[str] = None):
        self.accepted = accepted
        self.reason = reason

    def __bool__(self) -> bool:
        return self.accepted


def evaluate(leg: FlightLeg, state: Optional[Dict[str, Any]],
             now: Optional[datetime] = None) -> Verdict:
    """Should this aircraft be treated as flying this leg?"""
    if not state:
        return Verdict(True)

    now = now or datetime.now(timezone.utc)
    hex_id = (state.get("icao24") or "").strip().lower()

    # Positive identification, when the pilot has AeroAPI enabled.
    #
    # Everything below this is inference from callsign, schedule and
    # position. AeroAPI knows which flight record actually corresponds to
    # this origin/destination pair, so if it says the leg is over — gate-in
    # recorded, or cancelled — that is not a heuristic and it wins. This is
    # what finally settles the same-callsign turn: the outbound's own
    # record shows actual_in, so the return can never be adopted.
    enr = _enrichment_for(leg)
    if enr is not None:
        if enr.get("cancelled"):
            return Verdict(False, "AeroAPI reports this flight cancelled")
        if enr.get("actual_in"):
            return Verdict(False, "AeroAPI reports this leg arrived at the gate — "
                                  "anything on the callsign now is the next flight")
        # Registration is authoritative for WHICH airframe operates this
        # leg, so an ADS-B target on the right callsign but the wrong tail
        # is somebody else.
        reg = (enr.get("registration") or "").strip().upper()
        seen = (state.get("registration") or "").strip().upper()
        if reg and seen and reg != seen:
            return Verdict(False, f"AeroAPI has {reg} operating this leg, not {seen}")

    if is_complete(leg):
        return Verdict(False, "this leg has already finished — whatever is using "
                              "the callsign now belongs to the next flight")

    # When several legs share this callsign today, only the one actually in
    # progress may claim the aircraft. This is what covers a turn whose
    # outbound never registered as complete — typically because the
    # outstation has no ADS-B coverage, so the aircraft was never seen
    # stopped on the ground there.
    active = active_sibling(leg, now)
    if active is not None and active.id != leg.id:
        return Verdict(False, f"{active.flight_number} is operating "
                              f"{active.origin}-{active.destination} right now, "
                              f"not {leg.origin}-{leg.destination}")

    acquired = get_acquired_aircraft(leg)

    if acquired:
        if hex_id and hex_id != acquired:
            return Verdict(False, f"aircraft {hex_id} is not {acquired}, which is flying "
                                  f"this leg — same callsign, different aeroplane")
        # Locked on. Follow it anywhere: a diversion, a hold, or a return to
        # the departure airport are all still this leg's aircraft.
        return Verdict(True)

    lat, lon = state.get("lat"), state.get("lon")
    oi = leg.origin_info
    if lat is None or lon is None or not oi or oi.lat is None:
        return Verdict(True)  # nothing to judge on; don't block tracking

    d_origin = haversine_nm(lat, lon, oi.lat, oi.lon)
    at_origin = d_origin <= ACQUIRE_RADIUS_NM

    near_departure = False
    dep_utc = leg.dep_datetime_utc()
    if dep_utc:
        near_departure = (
            dep_utc - timedelta(minutes=ACQUIRE_BEFORE_DEP_MINUTES)
            <= now
            <= dep_utc + timedelta(minutes=ACQUIRE_AFTER_DEP_MINUTES)
        )

    if at_origin or near_departure:
        if hex_id:
            acquire_aircraft(leg, hex_id, now)
        return Verdict(True)

    return Verdict(False, f"{d_origin:.0f}nm from {leg.origin}, and past the departure "
                          f"window — waiting for an aircraft at the origin rather than "
                          f"adopting whatever is using this callsign")


def already_arrived(leg: FlightLeg, history: List[Dict[str, Any]], now: datetime) -> bool:
    """Has this leg finished?

    Once the aircraft has settled on the ground at the destination the leg
    is over, and anything appearing on the callsign afterwards belongs to
    the next flight. Imported locally to dodge a circular import.
    """
    from .track import compute_phase
    if not history:
        return False
    try:
        return compute_phase(leg, None, history, now, 15) == "Arrived"
    except Exception:
        return False


def is_plausible_for_leg(leg: FlightLeg, state: Optional[Dict[str, Any]],
                         now: Optional[datetime] = None) -> bool:
    return evaluate(leg, state, now).accepted


def rejection_reason(leg: FlightLeg, state: Optional[Dict[str, Any]],
                     now: Optional[datetime] = None) -> Optional[str]:
    return evaluate(leg, state, now).reason
