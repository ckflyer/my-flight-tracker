from datetime import datetime, date, time
from typing import List
import re
from .models import FlightLeg
from .airports import enrich_leg


# Supports:
# 06/26/2026 3729 DFW 1742 OKC 1837
# 08/04/2026 (D) 3232 PHX 1715 DFW 2150

LINE_RE = re.compile(
    r"""
    ^\s*
    (?P<date>\d{1,2}/\d{1,2}/\d{4})   # MM/DD/YYYY
    \s+
    (?:\(D\)\s+)?                     # optional deadhead marker
    (?P<flight>\d{1,4})               # flight number
    \s+
    (?P<origin>[A-Z]{3})              # origin IATA
    \s+
    (?P<dep>\d{3,4})                  # dep HHMM or HMM
    \s+
    (?P<dest>[A-Z]{3})                # dest IATA
    \s+
    (?P<arr>\d{3,4})                  # arr HHMM or HMM
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _parse_hhmm(s: str) -> time:
    s = s.zfill(4)
    h = int(s[:2])
    m = int(s[2:])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Invalid time: {s}")
    return time(h, m)


def parse_schedule_text(text: str) -> List[FlightLeg]:
    """
    Parse a multi-line FFDO schedule block into FlightLeg objects.
    Supports optional (D) deadhead marker before the flight number.
    """
    legs: List[FlightLeg] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group("date"), "%m/%d/%Y").date()
            flight = m.group("flight")
            origin = m.group("origin").upper()
            dest = m.group("dest").upper()
            dep = _parse_hhmm(m.group("dep"))
            arr = _parse_hhmm(m.group("arr"))
            is_deadhead = bool(re.search(r"\(D\)", line, re.IGNORECASE))

            leg_id = f"{d.isoformat()}-{flight}-{origin}-{dest}"
            if is_deadhead:
                leg_id += "-DH"

            leg = FlightLeg(
                id=leg_id,
                date=d,
                flight_number=flight,
                origin=origin,
                destination=dest,
                dep_time_local=dep,
                arr_time_local=arr,
                is_deadhead=is_deadhead,
            )
            enrich_leg(leg)
            legs.append(leg)
        except Exception:
            continue

    def sort_key(leg: FlightLeg):
        utc = leg.dep_datetime_utc()
        return utc or datetime.combine(leg.date, leg.dep_time_local)

    legs.sort(key=sort_key)
    return legs
