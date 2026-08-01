from datetime import datetime, timedelta, date
from typing import List, Optional
from zoneinfo import ZoneInfo

from .models import FlightLeg, Schedule, CurrentFlightInfo
from .parser import parse_schedule_text
from .airports import enrich_leg
from .db import get_connection, init_db

# Ensure tables exist (and legacy data/schedule.json is imported once) as soon
# as this module loads.
init_db()


def save_schedule(legs: List[FlightLeg]) -> None:
    """Persist the full leg list to SQLite, replacing whatever was there before."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM legs")
        for idx, leg in enumerate(legs):
            conn.execute(
                """
                INSERT OR REPLACE INTO legs
                    (id, sort_index, date, flight_number, origin, destination,
                     dep_time_local, arr_time_local, is_deadhead)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    leg.id,
                    idx,
                    leg.date.isoformat(),
                    leg.flight_number,
                    leg.origin,
                    leg.destination,
                    leg.dep_time_local.isoformat(),
                    leg.arr_time_local.isoformat(),
                    1 if leg.is_deadhead else 0,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def load_schedule() -> List[FlightLeg]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM legs ORDER BY sort_index ASC"
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()

    legs = []
    for row in rows:
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
            )
            enrich_leg(leg)
            legs.append(leg)
        except Exception:
            continue
    return legs


def import_from_text(text: str, replace: bool = True) -> List[FlightLeg]:
    new_legs = parse_schedule_text(text)
    if replace:
        save_schedule(new_legs)
        return new_legs
    else:
        existing = load_schedule()
        # merge by id
        by_id = {leg.id: leg for leg in existing}
        for leg in new_legs:
            by_id[leg.id] = leg
        merged = list(by_id.values())
        merged.sort(key=lambda l: l.dep_datetime_utc() or datetime.combine(l.date, l.dep_time_local))
        save_schedule(merged)
        return merged


def get_current_info(now: Optional[datetime] = None) -> CurrentFlightInfo:
    """
    Split schedule into past / current / upcoming based on schedule times.
    now should be timezone-aware (preferably UTC).

    The "current" window intentionally extends 3 hours past the scheduled
    arrival time (not just until the clock says it should have landed).
    Real flights run late, and taxi-in/landing detection needs the leg to
    still be "current" so live tracking keeps working through a delay,
    instead of the card vanishing into the past-flights list right when a
    delay makes it most useful.
    """
    if now is None:
        now = datetime.now(ZoneInfo("UTC"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))

    legs = load_schedule()
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

    # Past chronological (oldest first) so scrolling up through history feels natural
    past.sort(key=lambda l: l.dep_datetime_utc() or datetime.min)
    # Upcoming already chronological from original sort

    return CurrentFlightInfo(
        current=current,
        next=next_leg,
        past=past,
        upcoming=upcoming,
        all_legs=legs,
    )
