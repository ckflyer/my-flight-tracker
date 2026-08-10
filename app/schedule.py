"""Splitting the schedule into past / current / upcoming.

Storage lives in flights.py now — this is the time-based view of it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from .db import init_db
from .flights import (delete_leg, get_flight, legs_sharing_callsign,
                      load_schedule, save_schedule, set_operator_callsign)
from .models import CurrentFlightInfo, FlightLeg

# Ensure tables exist as soon as this module loads.
init_db()

__all__ = ["load_schedule", "save_schedule", "delete_leg", "set_operator_callsign",
           "legs_sharing_callsign", "get_current_info"]

# How long past SCHEDULED arrival a leg stays current on the clock alone.
CURRENT_GRACE = timedelta(hours=3)
# The hard ceiling on holding a leg current because it is still flying.
# Without it a row with a stuck airborne flag would own the card forever.
MAX_AIRBORNE_HOLD = timedelta(hours=12)


def _col(row, name):
    try:
        return row[name]
    except Exception:
        return None


def _still_flying(leg, now: datetime) -> bool:
    """Is this leg demonstrably STILL IN THE AIR, whatever the clock says?

    Deliberately narrow. It is true only while the aircraft is up and has
    not come down — airborne on ADS-B, or the airline published a wheels-off
    without a wheels-on. The moment it lands, this goes false and the
    ordinary clock takes over again.

    That distinction is the whole point. Two different failures were being
    treated as one:

      * A LEG THAT IS GENUINELY RUNNING LATE. Three hours past a scheduled
        arrival and still at 30,000 feet is a normal, if bad, day. The card
        used to drop it into past flights mid-cruise, which is precisely
        when the family is watching hardest.

      * A LEG THAT LANDED BUT NEVER CLOSED OUT. Gate-in is the OOOI field
        most often missing entirely, so a leg can sit open indefinitely
        with the aeroplane parked. Holding the card on THAT one is what
        stopped the next flight ever becoming current.

    Airborne holds. On the ground does not.
    """
    row = get_flight(leg.id)
    if row is None or _col(row, "closed"):
        return False
    arr = leg.arr_datetime_utc()
    if arr and now > arr + MAX_AIRBORNE_HOLD:
        return False
    # Down, by any source. The clock decides from here.
    if (_col(row, "landed_seen") or _col(row, "on_actual_api")
            or _col(row, "in_actual_api")):
        return False
    return bool(_col(row, "airborne_seen") or _col(row, "off_actual_api"))


def _has_started(leg, now: datetime) -> bool:
    """Real evidence this leg has begun — not just that its clock came up.

    Used to decide which of two overlapping candidates is the live one. A
    leg whose scheduled departure has merely arrived has NOT started; on a
    delayed day that is exactly the wrong flight to switch the card to.
    """
    row = get_flight(leg.id)
    if row is None or _col(row, "closed"):
        return False
    return bool(_col(row, "airborne_seen") or _col(row, "out_actual_api")
                or _col(row, "off_actual_api") or _col(row, "out_observed"))


def get_current_info(user_id: int, now: Optional[datetime] = None) -> CurrentFlightInfo:
    """Split this user's schedule by the clock, then let evidence override it.

    The "current" window runs from 20 minutes before departure to 3 hours
    past scheduled arrival. Real flights run late, and taxi-in detection
    needs the leg to still be current so live tracking keeps working through
    a delay — instead of the card vanishing into the past-flights list right
    when a delay makes it most useful.

    Two things sit on top of that window, and both exist because the clock
    alone got the handover wrong in opposite directions:

      1. A leg still demonstrably IN THE AIR stays current past the 3-hour
         grace (see _still_flying). One that has landed but never closed
         out does not — it releases the card and the next leg picks it up.

      2. When two legs qualify at once, a leg that has actually STARTED
         beats one that has merely reached its scheduled time. Without
         that, a 40-minute delay on leg 2 handed it the card while leg 1
         was still airborne, because leg 2's window opened first. With it,
         the card moves when the aeroplane moves.
    """
    if now is None:
        now = datetime.now(ZoneInfo("UTC"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))

    legs = load_schedule(user_id)
    if not legs:
        return CurrentFlightInfo()

    next_leg: Optional[FlightLeg] = None
    past: List[FlightLeg] = []
    upcoming: List[FlightLeg] = []
    candidates: List[FlightLeg] = []

    for leg in legs:
        dep_utc = leg.dep_datetime_utc()
        arr_utc = leg.arr_datetime_utc()
        if not dep_utc or not arr_utc:
            continue
        if now < dep_utc - timedelta(minutes=20):
            upcoming.append(leg)
            if next_leg is None:
                next_leg = leg
        elif now < arr_utc + CURRENT_GRACE or _still_flying(leg, now):
            candidates.append(leg)
        else:
            past.append(leg)

    # Prefer the latest leg with real evidence it has begun; otherwise the
    # latest by clock. `candidates` is already in schedule order.
    current: Optional[FlightLeg] = None
    started = [l for l in candidates if _has_started(l, now)]
    if started:
        current = started[-1]
    elif candidates:
        current = candidates[-1]

    # Anything that lost the contest is over, not pending — it is behind the
    # leg now flying. Without this a superseded candidate would simply
    # vanish from all three lists.
    for leg in candidates:
        if current is None or leg.id != current.id:
            past.append(leg)

    past.sort(key=lambda l: l.dep_datetime_utc() or datetime.min.replace(tzinfo=ZoneInfo("UTC")))
    return CurrentFlightInfo(current=current, next=next_leg, past=past,
                             upcoming=upcoming, all_legs=legs)
