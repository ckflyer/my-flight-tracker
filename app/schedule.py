from datetime import datetime, timedelta, date
from typing import List, Optional
from zoneinfo import ZoneInfo

from .models import FlightLeg, Schedule, CurrentFlightInfo
from .parser import parse_schedule_text
from .airports import enrich_leg
import json
from pathlib import Path


DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "schedule.json"


def save_schedule(legs: List[FlightLeg]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "legs": [leg.model_dump(mode="json") for leg in legs],
    }
    DATA_FILE.write_text(json.dumps(payload, indent=2, default=str))


def load_schedule() -> List[FlightLeg]:
    if not DATA_FILE.exists():
        return []
    try:
        data = json.loads(DATA_FILE.read_text())
        legs = []
        for item in data.get("legs", []):
            # reconstruct date/time objects
            item["date"] = date.fromisoformat(item["date"])
            item["dep_time_local"] = datetime.strptime(item["dep_time_local"], "%H:%M:%S").time()
            item["arr_time_local"] = datetime.strptime(item["arr_time_local"], "%H:%M:%S").time()
            leg = FlightLeg(**item)
            enrich_leg(leg)
            legs.append(leg)
        return legs
    except Exception:
        return []


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

    for leg in legs:
        dep_utc = leg.dep_datetime_utc()
        arr_utc = leg.arr_datetime_utc()
        if not dep_utc or not arr_utc:
            continue

        status = leg.status_at(now)

        if status in ("Departing", "In Air"):
            current = leg
        elif status == "Arrived":
            past.append(leg)
        else:  # Scheduled
            upcoming.append(leg)
            if next_leg is None and dep_utc > now:
                next_leg = leg

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
