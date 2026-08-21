"""Splitting the schedule into past / current / upcoming.

Storage lives in flights.py now — this is the time-based view of it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from .db import init_db
from .flights import (delete_leg, get_flight, legs_sharing_callsign,
                      load_schedule, merge_schedule, remove_legs,
                      replace_schedule, set_operator_callsign)
from .models import CurrentFlightInfo, FlightLeg

# Ensure tables exist as soon as this module loads.
init_db()

__all__ = ["load_schedule", "merge_schedule", "remove_legs", "replace_schedule",
           "delete_leg", "set_operator_callsign", "legs_sharing_callsign",
           "get_current_info", "CURRENT_GRACE"]

# How long past SCHEDULED arrival a leg stays current on the clock alone.
CURRENT_GRACE = timedelta(hours=3)
# How long before SCHEDULED departure a leg stops being "upcoming" and
# becomes eligible for the card. Named rather than repeated: _window_open
# and the split below must agree exactly, or a leg falls into no list.
CURRENT_WINDOW_BEFORE_DEP = timedelta(minutes=20)
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


def _on_ground(leg, now: datetime) -> bool:
    """Any indication at all that this leg's aeroplane is DOWN. (1.5.0)

    Owner's rule, and the right one: "if there is any indication of the
    flight being on the ground, and the next flight is scheduled to be
    active, then it needs to cycle to the next flight."

    Deliberately BROAD, and that is the opposite of how the rest of this
    file reasons. Everywhere else — `has_departed`, `_has_started`,
    closure's guards — the app demands strong evidence, because the cost of
    being wrong is closing a flight that is still going. Here the cost runs
    the other way. Being wrong means showing a family member a finished leg
    while the pilot is boarding the next one, and the recovery is automatic:
    the moment the aeroplane is airborne again `_still_flying` takes over
    and the card is right anyway. So any one of six signals is enough.

    The last of them matters most in practice. `landed_seen` needs a
    sustained touchdown to be OBSERVED, which a station with no ground
    coverage never provides; but an aircraft that we watched get airborne
    and are now seeing on the ground has landed, whatever the confirmation
    timer thinks.
    """
    row = get_flight(leg.id)
    if row is None:
        return False
    if _col(row, "closed"):
        return True
    for key in ("landed_seen", "on_actual_api", "in_actual_api", "in_observed"):
        if _col(row, key):
            return True
    return bool(_col(row, "airborne_seen") and _col(row, "last_on_ground"))


def _window_open(leg, now: datetime) -> bool:
    """Has this leg reached the point where it could be the live one?

    Same T-20 boundary `get_current_info` uses to move a leg out of
    `upcoming`, so a leg can only ever take the card at the moment it stops
    being upcoming. Derived rather than repeated, because two independent
    copies of this number drifting apart would create a gap where a leg is
    in neither list.
    """
    dep = leg.dep_datetime_utc()
    return bool(dep and now >= dep - CURRENT_WINDOW_BEFORE_DEP)


def _pick_current(candidates: List[FlightLeg], now: datetime) -> Optional[FlightLeg]:
    """Which of the overlapping candidates is the live one.

    Three rules, applied in order, each fixing a failure the ones above it
    produced:

      1. A leg with real evidence of having DEPARTED beats one that has
         merely reached its scheduled time. Without this a 40-minute delay
         on leg 2 took the card off an airborne leg 1, because leg 2's
         window opened first.

      2. ...but a leg that is DOWN hands the card on as soon as the next
         leg's window opens. (1.5.0) Rule 1 on its own meant a landed leg
         held the card for the full three-hour grace while the crew were
         already boarding the next one — the closeout fix in this same
         release made legs close properly and did NOT fix this, because
         selection never asked whether the leg had closed. It asked only
         which leg had started, and a finished leg has still started.

      3. With no evidence anywhere, the clock decides and the latest
         qualifying leg wins.
    """
    if not candidates:
        return None
    started = [l for l in candidates if _has_started(l, now)]
    if not started:
        return candidates[-1]

    best = started[-1]
    if not _on_ground(best, now):
        return best

    # It is down. Hand over to the earliest later candidate whose own
    # window has opened — the NEXT leg, not the last one of the day, so a
    # long duty period steps forward one leg at a time.
    order = {id(l): i for i, l in enumerate(candidates)}
    for leg in candidates:
        if order[id(leg)] > order[id(best)] and _window_open(leg, now):
            return leg
    return best


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
        if now < dep_utc - CURRENT_WINDOW_BEFORE_DEP:
            upcoming.append(leg)
            if next_leg is None:
                next_leg = leg
        elif now < arr_utc + CURRENT_GRACE or _still_flying(leg, now):
            candidates.append(leg)
        else:
            past.append(leg)

    # Which of the overlapping candidates is live. `candidates` is already
    # in schedule order. See _pick_current for the three rules.
    current: Optional[FlightLeg] = _pick_current(candidates, now)

    # Anything that lost the contest is over, not pending — it is behind the
    # leg now flying. Without this a superseded candidate would simply
    # vanish from all three lists.
    for leg in candidates:
        if current is None or leg.id != current.id:
            past.append(leg)

    past.sort(key=lambda l: l.dep_datetime_utc() or datetime.min.replace(tzinfo=ZoneInfo("UTC")))
    return CurrentFlightInfo(current=current, next=next_leg, past=past,
                             upcoming=upcoming, all_legs=legs)
