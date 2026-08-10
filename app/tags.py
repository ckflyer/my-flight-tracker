"""The two pills: what the aeroplane is doing, and whether the plan holds.

PHASE answers "where is he right now" and comes from a ladder that only
ever moves FORWARD:

    Scheduled -> Taxi-out -> In air -> Landing -> Taxi-in -> Arrived

Forward-only is the fix for the single most visible bug in v4. ADS-B
coverage drops somewhere over west Texas, the old phase machine returned
"Unknown", and the card went from "In air" to "Unknown" — so to the person
watching, the app forgot where he was. Nothing had happened. We had just
stopped hearing. Losing the signal is a fact about our RECEPTION, not
about the aeroplane, and it now shows as a note ("no signal for 14 min")
beside a phase that stays put.

The old "Departing" phase is gone for the same reason it was removed in
v4.3: scheduled departure passing is not evidence the aircraft moved.

STATUS answers "is the plan holding" and is BLANK when there is nothing to
say. There is no "On time" pill — a green badge on every normal flight is
wallpaper, and the eye stops seeing it. Only the status pill carries
colour, so colour always means something.

    Cancelled   airline says so. Sticks. Hides the phase pill entirely —
                the aeroplane isn't doing anything, so "Scheduled" beside
                "Cancelled" is noise.
    Diverted    airline says so, or we watched it stop somewhere that
                isn't the destination. Sticks: a flight that diverted
                diverted, even if a later leg reaches the original field.
    Delayed     see below.

Status, unlike phase, moves BOTH WAYS. If the airline pushes departure to
14:20 and then pulls it back to 13:55, the pill should go back to blank —
that isn't the app forgetting, it's a real improvement in his day.
Cancelled and Diverted are the exceptions and never clear.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from .geo import haversine_nm

# In air -> Landing inside this range. 17 nm fired while still being
# vectored downwind; 8 is about final.
LANDING_RADIUS_NM = 8.0

# Below this the aircraft is parked, not taxiing.
GROUND_STOP_KTS = 5.0

# How far the airline has to push a time past the FFDO figure before the
# Delayed pill lights up. One minute, purely so clock rounding between two
# sources can't flicker the pill on and off.
DELAY_MIN_MINUTES = float(os.environ.get("DELAY_MIN_MINUTES", "1"))

PHASE_SCHEDULED = "Scheduled"
PHASE_TAXI_OUT = "Taxi-out"
PHASE_IN_AIR = "In air"
PHASE_LANDING = "Landing"
PHASE_TAXI_IN = "Taxi-in"
PHASE_ARRIVED = "Arrived"

PHASE_ORDER = {
    PHASE_SCHEDULED: 0,
    PHASE_TAXI_OUT: 1,
    PHASE_IN_AIR: 2,
    PHASE_LANDING: 3,
    PHASE_TAXI_IN: 4,
    PHASE_ARRIVED: 5,
}

STATUS_CANCELLED = "Cancelled"
STATUS_DIVERTED = "Diverted"
STATUS_DELAYED = "Delayed"
STICKY_STATUS = (STATUS_CANCELLED, STATUS_DIVERTED)


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _col(row, name):
    """Read a column that may not exist on an older row object."""
    try:
        return row[name]
    except Exception:
        return None


# ----------------------------------------------------------------- phase
def compute_phase(row, leg, now: datetime) -> str:
    """The phase this observation implies, BEFORE the forward-only guard.

    Every branch here reads either something the aircraft broadcast or
    something the airline published. Nothing is inferred from the clock,
    and nothing is inferred from silence.
    """
    if _col(row, "closed"):
        return PHASE_ARRIVED

    # SQLite hands back 0/1, not False/True, so `is False` never matches a
    # stored value. Normalise once here rather than at four comparison
    # sites — that mismatch silently sent every airborne aircraft down the
    # airline-data branch instead of the position branch, so "Landing" was
    # unreachable for anyone without an AeroAPI key.
    raw_ground = _col(row, "last_on_ground")
    on_ground = None if raw_ground is None else bool(raw_ground)
    lat, lon = _col(row, "last_lat"), _col(row, "last_lon")
    airborne_seen = bool(_col(row, "airborne_seen"))

    wheels_on = _col(row, "on_actual_api") or _col(row, "on_observed")
    wheels_off = _col(row, "off_actual_api") or _col(row, "off_observed")
    gate_out = _col(row, "out_actual_api") or _col(row, "out_observed")

    # Taxi-in: it is down, and still on the ground. Either source will do —
    # the airline publishes wheels-on with a lag, ADS-B misses it entirely
    # where there's no receiver, so whichever notices first wins.
    if wheels_on and (on_ground is None or on_ground):
        return PHASE_TAXI_IN
    if airborne_seen and on_ground:
        return PHASE_TAXI_IN

    # Airborne. Landing only inside the ring, and only with a real fix —
    # "probably on approach by now" is exactly the guessing this replaced.
    if on_ground is False:
        dest = leg.dest_info
        if dest and dest.lat is not None and lat is not None and lon is not None:
            if haversine_nm(lat, lon, dest.lat, dest.lon) <= LANDING_RADIUS_NM:
                return PHASE_LANDING
        return PHASE_IN_AIR

    # The airline's own view of the same question, for legs with no ADS-B
    # coverage at all — which is exactly when it matters most.
    if wheels_off and not wheels_on:
        return PHASE_IN_AIR
    if gate_out and not wheels_off:
        return PHASE_TAXI_OUT

    # On the ground and moving, having never been airborne: pushed back.
    speed = _col(row, "last_speed_kts")
    if on_ground and speed is not None and speed > GROUND_STOP_KTS and not airborne_seen:
        return PHASE_TAXI_OUT

    return PHASE_SCHEDULED


def advance_phase(previous: Optional[str], candidate: str) -> str:
    """Forward-only. The ladder never goes back down."""
    if not previous:
        return candidate
    if previous not in PHASE_ORDER:
        return candidate
    if candidate not in PHASE_ORDER:
        return previous
    return candidate if PHASE_ORDER[candidate] > PHASE_ORDER[previous] else previous


# ---------------------------------------------------------------- status
def _revision_minutes(scheduled_api, estimated, actual, ffdo: Optional[datetime]
                      ) -> Optional[int]:
    """How far past the FFDO time the airline has PUSHED this event.

    Two conditions, and both are needed:

      1. The airline must have actually moved something. Its published
         schedule and the pilot's bid line routinely differ by a few
         minutes with nothing wrong — comparing the published time
         straight to the FFDO time would leave the pill permanently lit.
         So there has to be a real revision: an estimate (or an actual)
         that differs from the airline's own scheduled time.

      2. The revised time must land later than the FFDO time. He flies the
         bid line, so that is what "late" means here.

    Returns positive minutes when both hold, otherwise None.
    """
    if ffdo is None:
        return None
    sched_api = _parse(scheduled_api)
    revised = _parse(actual) or _parse(estimated)
    if revised is None:
        return None
    # No push from the airline means no delay, however the two schedules
    # happen to line up.
    if sched_api is not None and abs((revised - sched_api).total_seconds()) < 60:
        return None
    if sched_api is None and _parse(estimated) is None:
        return None
    minutes = (revised - ffdo).total_seconds() / 60
    if minutes < DELAY_MIN_MINUTES:
        return None
    return int(minutes)


def compute_status(row, leg, now: datetime) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """(status_tag, dep_revision_min, arr_revision_min).

    status_tag is None when there is nothing worth a pill.
    """
    previous = _col(row, "status_tag")

    if _col(row, "cancelled"):
        return STATUS_CANCELLED, None, None
    if _col(row, "diverted"):
        return STATUS_DIVERTED, None, None
    # Sticky: once either of those has been true it stays true, even if a
    # later API record no longer mentions it.
    if previous in STICKY_STATUS:
        return previous, None, None

    dep_rev = _revision_minutes(
        _col(row, "out_scheduled"), _col(row, "out_estimated"),
        _col(row, "out_actual_api"), leg.dep_datetime_utc())
    arr_rev = _revision_minutes(
        _col(row, "in_scheduled"), _col(row, "in_estimated"),
        _col(row, "in_actual_api"), leg.arr_datetime_utc())

    # Before pushback the question is "is he getting out". After it, the
    # question is "when does he get there" — a late departure that makes
    # up the time enroute should stop showing a pill.
    gate_out = _col(row, "out_actual_api") or _col(row, "out_observed")
    active = arr_rev if gate_out else dep_rev

    if _col(row, "closed"):
        # A finished flight is judged on where it actually got in.
        active = arr_rev

    return (STATUS_DELAYED if active else None), dep_rev, arr_rev


# ------------------------------------------------------------------ note
def signal_note(row, now: datetime) -> Optional[str]:
    """"no signal for 14 min", or None while we're hearing from it.

    This is what replaced the Unknown phase. It says what is actually true
    — that WE have lost contact — without pretending the flight changed
    state.
    """
    if _col(row, "closed"):
        return None
    last = _parse(_col(row, "last_signal_at"))
    if last is None:
        return None
    gap = (now - last).total_seconds() / 60
    if gap < 4:
        return None
    if gap < 90:
        return f"no signal for {int(gap)} min"
    return f"no signal for {int(gap // 60)}h {int(gap % 60):02d}m"


def never_tracked(row, leg, now: datetime) -> bool:
    """True only when we have literally never heard anything about this
    flight and the airline hasn't spoken either, and it should have gone
    by now. This is the honest version of the old Unknown."""
    if _col(row, "last_signal_at") or _col(row, "last_api_query_at"):
        return False
    dep = leg.dep_datetime_utc()
    return bool(dep and now > dep + timedelta(minutes=20))
