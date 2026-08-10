"""Splitting the schedule into past / current / upcoming.

Storage lives in flights.py now — this is the time-based view of it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from .db import init_db
from .flights import (delete_leg, legs_sharing_callsign, load_schedule,
                      save_schedule, set_operator_callsign)
from .models import CurrentFlightInfo, FlightLeg

# Ensure tables exist as soon as this module loads.
init_db()

__all__ = ["load_schedule", "save_schedule", "delete_leg", "set_operator_callsign",
           "legs_sharing_callsign", "get_current_info"]


def get_current_info(user_id: int, now: Optional[datetime] = None) -> CurrentFlightInfo:
    """Split this user's schedule by the clock.

    The "current" window intentionally extends 3 hours past scheduled
    arrival. Real flights run late, and taxi-in detection needs the leg to
    still be current so live tracking keeps working through a delay —
    instead of the card vanishing into the past-flights list right when a
    delay makes it most useful.
    """
    if now is None:
        now = datetime.now(ZoneInfo("UTC"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))

    legs = load_schedule(user_id)
    if not legs:
        return CurrentFlightInfo()

    current: Optional[FlightLeg] = None
    next_leg: Optional[FlightLeg] = None
    past: List[FlightLeg] = []
    upcoming: List[FlightLeg] = []
    CURRENT_GRACE = timedelta(hours=3)

    for leg in legs:
        dep_utc = leg.dep_datetime_utc()
        arr_utc = leg.arr_datetime_utc()
        if not dep_utc or not arr_utc:
            continue
        if now < dep_utc - timedelta(minutes=20):
            upcoming.append(leg)
            if next_leg is None:
                next_leg = leg
        elif now < arr_utc + CURRENT_GRACE:
            current = leg
        else:
            past.append(leg)

    past.sort(key=lambda l: l.dep_datetime_utc() or datetime.min.replace(tzinfo=ZoneInfo("UTC")))
    return CurrentFlightInfo(current=current, next=next_leg, past=past,
                             upcoming=upcoming, all_legs=legs)
