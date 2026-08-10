"""Which physical aircraft is flying this leg?

Live data is looked up by callsign, and a callsign is not unique to a leg.
Regional turns fly out and back under one flight number — 3700 DFW-MFE and
3700 MFE-DFW the same day — and the return departs well inside the window
the outbound leg stays current for. A plain callsign match locks onto the
wrong aircraft, and with the background poller running it writes an entire
return flight into the outbound leg's stored track.

This module is UNCHANGED IN BEHAVIOUR from v4. It is the most correct part
of the app and it earned that the hard way. Only its storage moved: the
old `flight_aircraft` table is now columns on the flight row.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not judge an aircraft by which way it is pointing. An earlier
version rejected aircraft heading away from the destination, which breaks
the moment a flight diverts: a DFW-OKC leg turning back to DFW looks
exactly like the return flight, so the guard disowned the user's own
aeroplane at precisely the moment anyone watching most needed to see it.
Holds, opposite-flow departures and arrival vectoring break it the same
way. Geometry cannot tell "going somewhere else" apart from "going
somewhere else on purpose". There is no route data to compare against
either — ADS-B does not transmit origin or destination.

WHAT IT DOES INSTEAD
--------------------
Identity. Every aircraft broadcasts a unique ICAO 24-bit hex address.

  0. ARBITRATE — when several legs share a callsign on the same day, only
     the one actually in progress may claim the aircraft: the latest leg
     whose scheduled departure has arrived. Deterministic, and needs no
     observation of the aircraft at all — which matters because the
     ground-cycle release below depends on seeing it stopped at the
     outstation, and small fields frequently have no ADS-B coverage.
  1. ACQUIRE — a leg adopts an aircraft on its callsign when that aircraft
     is at the ORIGIN, or during a window around scheduled departure. The
     window covers outstations with no receiver, where a flight may first
     appear already enroute. A turn's return leg can't depart until this
     one has landed, so it never falls inside that window.
  2. HOLD — from then on only that hex is accepted, unconditionally,
     wherever it goes. Diversions, holds and returns to the departure
     field are all followed.
  3. RELEASE — a leg is one ground -> airborne -> ground cycle. Once the
     aircraft has flown and then been STOPPED for GROUND_COMPLETE_SECONDS
     the leg is finished. Stopped rather than merely on the ground, so a
     long taxi-in doesn't end the leg early and lose the taxi track.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .flights import flight_key, get_flight, legs_sharing_callsign, write
from .geo import haversine_nm
from .models import FlightLeg

# How close to the origin an aircraft must be before a leg adopts it.
# Generous enough to cover pushback, taxi, takeoff and initial climb.
ACQUIRE_RADIUS_NM = float(os.environ.get("ACQUIRE_RADIUS_NM", "30"))

# Requiring an aircraft to be SEEN at the origin is precise but brittle:
# plenty of outstations have no ADS-B receiver nearby, so a flight can
# depart, climb out and be halfway to the destination before it appears in
# the feed. With acquisition tied strictly to the origin, that leg would
# never be tracked at all. So there is a second way in: around scheduled
# departure, adopt the aircraft on this callsign wherever it is. This
# stays safe against the turn problem for timing reasons — the return leg
# can't depart until this one has landed and turned around.
ACQUIRE_AFTER_DEP_MINUTES = float(os.environ.get("ACQUIRE_AFTER_DEP_MINUTES", "45"))
ACQUIRE_BEFORE_DEP_MINUTES = float(os.environ.get("ACQUIRE_BEFORE_DEP_MINUTES", "20"))

GROUND_STOP_KTS = 5.0
# Touchdown confirmation. A single frame reporting alt_baro="ground" on
# approach would otherwise fire wheels-down early, so it has to hold this
# long AND be slow enough to be a real rollout rather than a glitch.
LANDING_CONFIRM_SECONDS = 60.0
LANDING_MAX_KTS = 90.0
GROUND_COMPLETE_SECONDS = float(os.environ.get("GROUND_COMPLETE_SECONDS", "300"))


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _col(row, name):
    try:
        return row[name]
    except Exception:
        return None


def active_sibling(leg: FlightLeg, now: datetime) -> Optional[FlightLeg]:
    """Of the legs sharing this callsign today, which one is flying NOW?

    Asking "is this aircraft mine?" leg-by-leg cannot answer that — both
    directions of a turn see the same callsign and the same airframe. The
    schedule can, and deterministically: the leg in progress is the LATEST
    one whose scheduled departure has arrived.
    """
    siblings = legs_sharing_callsign(leg.flight_number, leg.date)
    if len(siblings) < 2:
        return None
    cutoff = now + timedelta(minutes=ACQUIRE_BEFORE_DEP_MINUTES)
    started = [l for l in siblings if (l.dep_datetime_utc() or now) <= cutoff]
    if not started:
        return siblings[0]
    return max(started, key=lambda l: l.dep_datetime_utc() or now)


# ------------------------------------------------------------- observing
def observe(leg: FlightLeg, state: Dict[str, Any], now: datetime, row=None) -> None:
    """Fold one live observation into the leg's flight-cycle state.

    Two signals, and they only mean anything together:

      * GROUND CYCLE — the aircraft has flown, and is now stopped on the
        ground. Stopped, not merely on the ground, so a long taxi-in
        doesn't end the leg early.
      * SQUAWK — a new transponder code seen WHILE STOPPED ON THE GROUND
        means a new flight plan, so the previous leg is over. Only ever
        consulted on the ground: codes are routinely reassigned in flight
        as an aircraft is handed between ATC facilities, so an airborne
        change says nothing about whether the flight has ended.
    """
    row = row if row is not None else get_flight(leg.id)
    if row is None:
        return

    on_ground = state.get("on_ground")
    speed = state.get("speed_kts")
    squawk = state.get("squawk")

    airborne_seen = bool(_col(row, "airborne_seen"))
    landed_seen = bool(_col(row, "landed_seen"))
    landing_since = _col(row, "landing_since")
    stopped_since = _col(row, "stopped_since")
    last_squawk = _col(row, "last_squawk")

    relaunched = False
    newly_airborne = False
    newly_landed = False
    new_stopped_since = stopped_since
    new_landing_since = landing_since
    squawk_change_parked = False

    if on_ground is False:
        # Airborne again after having landed: this leg is over and the
        # aeroplane is flying the next one. Unambiguous, and free.
        if landed_seen:
            relaunched = True
        if not airborne_seen:
            newly_airborne = True
        airborne_seen = True
        new_stopped_since = None      # airborne: no ground timer running
        new_landing_since = None      # any touchdown attempt is void
    elif on_ground is True:
        # Touchdown has to be sustained before it counts.
        if airborne_seen and not landed_seen:
            plausible = speed is None or speed <= LANDING_MAX_KTS
            if not plausible:
                new_landing_since = None
            elif new_landing_since is None:
                new_landing_since = now.isoformat()
            else:
                held = _parse(new_landing_since)
                if held and (now - held).total_seconds() >= LANDING_CONFIRM_SECONDS:
                    landed_seen = True
                    newly_landed = True
        stopped = speed is not None and speed <= GROUND_STOP_KTS
        if stopped:
            if new_stopped_since is None:
                new_stopped_since = now.isoformat()
            if airborne_seen and squawk and last_squawk and squawk != last_squawk:
                squawk_change_parked = True
        else:
            new_stopped_since = None  # taxiing, not finished

    # Everything here is a fact about the AEROPLANE, so it lands on the
    # shared row and every crew member on the leg sees it at once.
    write(
        leg.id,
        once={
            # Backdate wheels-off/on to the moment they happened, not to
            # when confirmation finished.
            "off_observed": now.isoformat() if newly_airborne else None,
            "on_observed": new_landing_since if newly_landed else None,
        },
        latest={
            "last_lat": state.get("lat"),
            "last_lon": state.get("lon"),
            "last_altitude_ft": state.get("altitude_ft"),
            "last_speed_kts": speed,
            "last_track": state.get("track"),
            "last_fix_age_s": state.get("position_age_s"),
            "tail_adsb": state.get("registration"),
            "type_code": state.get("type_code"),
            "aircraft_type": state.get("aircraft_type"),
            # Only ever remember a squawk observed on the ground, so an
            # in-flight reassignment can't poison the comparison.
            "last_squawk": squawk if on_ground else None,
        },
        always={
            "last_on_ground": None if on_ground is None else int(on_ground),
            "last_signal_at": now.isoformat(),
            "airborne_seen": int(airborne_seen),
            "landed_seen": int(landed_seen),
            "landing_since": new_landing_since,
            "stopped_since": new_stopped_since,
            "relaunched": int(bool(_col(row, "relaunched")) or relaunched
                              or squawk_change_parked),
        },
    )


def observed_gate_in(row) -> Optional[datetime]:
    """When WE saw the aircraft come to a stop after flying.

    The closest thing to the parking-brake moment ADS-B can give, and it
    beats a stale estimated_in: the airline publishes actual_in with a lag,
    so a flight can be sitting at the gate while the only airline figure
    available is an hour-old forecast. Showing that forecast as the arrival
    time is how "arrived 4:05, 11 minutes early" appeared for a flight that
    actually blocked in at 4:11.
    """
    if row is None or not _col(row, "airborne_seen"):
        return None
    return _parse(_col(row, "stopped_since"))


def stopped_seconds(row, now: datetime) -> Optional[float]:
    stopped = observed_gate_in(row)
    return None if stopped is None else (now - stopped).total_seconds()


def signal_gap_seconds(row, now: datetime) -> Optional[float]:
    last = _parse(_col(row, "last_signal_at"))
    return None if last is None else (now - last).total_seconds()


def wheels_down_at(row) -> Optional[datetime]:
    """When the aircraft touched down, if we saw it or the airline said."""
    if row is None:
        return None
    return _parse(_col(row, "on_actual_api")) or _parse(_col(row, "on_observed"))


def took_off_at(row) -> Optional[datetime]:
    if row is None:
        return None
    return _parse(_col(row, "off_actual_api")) or _parse(_col(row, "off_observed"))


def has_flown(row) -> bool:
    """Is there real evidence this flight got airborne? Not the clock."""
    if row is None:
        return False
    return bool(_col(row, "airborne_seen")) or bool(_col(row, "off_actual_api"))


# ----------------------------------------------------------- acquisition
def acquire_aircraft(leg: FlightLeg, icao24: str, now: datetime) -> None:
    """Lock this leg to one airframe. Written once, on the shared row."""
    write(leg.id, once={"aircraft_hex": icao24,
                        "aircraft_acquired_at": now.isoformat()})


class Verdict:
    def __init__(self, accepted: bool, reason: Optional[str] = None):
        self.accepted = accepted
        self.reason = reason

    def __bool__(self) -> bool:
        return self.accepted


def evaluate(leg: FlightLeg, state: Optional[Dict[str, Any]],
             now: Optional[datetime] = None, row=None) -> Verdict:
    """Should this aircraft be treated as flying this leg?"""
    if not state:
        return Verdict(True)
    now = now or datetime.now(timezone.utc)
    row = row if row is not None else get_flight(leg.id)
    hex_id = (state.get("icao24") or "").strip().lower()

    if row is not None:
        # A closed leg is frozen and accepts nothing further, from any
        # source. This is also what permanently retires the same-callsign
        # turn problem: a closed leg can never adopt the return flight.
        if _col(row, "closed"):
            return Verdict(False, "this leg is closed out — anything on the callsign "
                                  "now belongs to a later flight")
        if _col(row, "cancelled"):
            return Verdict(False, "the airline reports this flight cancelled")
        # Positive identification, when a pilot has AeroAPI enabled.
        # Everything else here is inference from callsign, schedule and
        # position; the airline knows which record corresponds to this
        # origin/destination pair, so if it says the leg is over, that is
        # not a heuristic and it wins.
        if _col(row, "in_actual_api"):
            return Verdict(False, "the airline reports this leg at the gate — "
                                  "anything on the callsign now is the next flight")
        if _col(row, "relaunched"):
            return Verdict(False, "this leg has already finished — whatever is using "
                                  "the callsign now belongs to the next flight")
        # Registration is authoritative for WHICH airframe operates this
        # leg, so a target on the right callsign but the wrong tail is
        # somebody else.
        reg = (_col(row, "tail_api") or "").strip().upper()
        seen = (state.get("registration") or "").strip().upper()
        if reg and seen and reg != seen:
            return Verdict(False, f"the airline has {reg} operating this leg, not {seen}")

    # When several legs share this callsign today, only the one actually
    # in progress may claim the aircraft. This covers a turn whose
    # outbound never registered as complete — typically because the
    # outstation has no ADS-B coverage.
    active = active_sibling(leg, now)
    if active is not None and active.id != leg.id:
        return Verdict(False, f"{active.flight_number} is operating "
                              f"{active.origin}-{active.destination} right now, "
                              f"not {leg.origin}-{leg.destination}")

    acquired = (_col(row, "aircraft_hex") or "").strip().lower() if row is not None else ""
    if acquired:
        if hex_id and hex_id != acquired:
            return Verdict(False, f"aircraft {hex_id} is not {acquired}, which is flying "
                                  f"this leg — same callsign, different aeroplane")
        # Locked on. Follow it anywhere: a diversion, a hold, or a return
        # to the departure airport are all still this leg's aircraft.
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
