"""When to spend a query, and what to do with the answer.

The pilot's own AeroAPI key pays for this, so the budget rules matter as
much as the data:

  * ONLY the background poller refreshes enrichment. Page renders read the
    cache and never trigger a fetch, so a family member hitting refresh
    fifty times during a delay costs nothing.
  * ADS-B transitions are the primary trigger. Wheels-up, wheels-down and
    stopped-at-the-gate are already detected for free, so a query is spent
    when something actually changed rather than on a timer.
  * A schedule-based fallback covers the case that breaks the above: no
    ADS-B coverage at the outstation means no transitions to react to.
  * Hard caps per leg and a minimum gap between queries, so no bug can run
    up someone's bill.

Budget works out around four to six queries per leg.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional

from .aeroapi import AeroApiError, fetch_leg
from .db import get_connection

# Never query a leg more often than this, whatever the triggers say.
MIN_QUERY_GAP = timedelta(minutes=8)
# Absolute ceiling per leg — a runaway loop can't cost more than this.
MAX_QUERIES_PER_LEG = 10
# How early to take a first look (gate assignment and any pre-departure delay).
PREVIEW_BEFORE_DEP = timedelta(minutes=60)
# If ADS-B has told us nothing by this far past scheduled departure, ask anyway.
SILENT_DEP_FALLBACK = timedelta(minutes=15)
# Start watching for arrival details this long before the estimate.
ARRIVAL_WATCH = timedelta(minutes=25)


# --------------------------------------------------------------- storage
def get_enrichment(user_id: int, leg_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT payload, fetched_at FROM flight_enrichment WHERE leg_id = ? AND user_id = ?",
            (leg_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        data = json.loads(row["payload"])
        data["_fetched_at"] = row["fetched_at"]
        return data
    except Exception:
        return None


def _store(user_id: int, leg_id: str, payload: Dict[str, Any], now: datetime,
           raw: Optional[Dict[str, Any]] = None) -> None:
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT first_seen FROM flight_enrichment WHERE leg_id = ? AND user_id = ?",
            (leg_id, user_id),
        ).fetchone()
        # Snapshot the first values seen and never overwrite them — that's
        # the only record of the ORIGINAL schedule once an airline amends it.
        first_seen = existing["first_seen"] if existing and existing["first_seen"] else json.dumps(payload)
        conn.execute(
            "INSERT OR REPLACE INTO flight_enrichment "
            "(leg_id, user_id, fetched_at, payload, raw, first_seen) VALUES (?, ?, ?, ?, ?, ?)",
            (leg_id, user_id, now.isoformat(), json.dumps(payload),
             json.dumps(raw) if raw is not None else None, first_seen),
        )
        conn.commit()
    finally:
        conn.close()


def get_first_seen(user_id: int, leg_id: str) -> Optional[Dict[str, Any]]:
    """The earliest snapshot of this leg, before any schedule amendments."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT first_seen FROM flight_enrichment WHERE leg_id = ? AND user_id = ?",
            (leg_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["first_seen"]:
        return None
    try:
        return json.loads(row["first_seen"])
    except Exception:
        return None


def _count_query(user_id: int, now: datetime) -> None:
    """Bump this month's query counter, rolling over on a new month."""
    period = now.strftime("%Y-%m")
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT aeroapi_period, aeroapi_queries FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row or row["aeroapi_period"] != period:
            conn.execute(
                "UPDATE users SET aeroapi_period = ?, aeroapi_queries = 1 WHERE id = ?",
                (period, user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET aeroapi_queries = aeroapi_queries + 1 WHERE id = ?",
                (user_id,),
            )
        conn.commit()
    finally:
        conn.close()


def query_stats(user_id: int) -> Dict[str, Any]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT aeroapi_period, aeroapi_queries FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    if not row or row["aeroapi_period"] != period:
        return {"period": period, "queries": 0}
    return {"period": row["aeroapi_period"], "queries": row["aeroapi_queries"] or 0}


def credentials(user_id: int) -> Optional[str]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT aeroapi_enabled, aeroapi_key FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["aeroapi_enabled"]:
        return None
    key = (row["aeroapi_key"] or "").strip()
    return key or None


# ------------------------------------------------------------- decisions
def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def is_finished(enr: Optional[Dict[str, Any]]) -> bool:
    """Nothing more to learn about this leg."""
    if not enr:
        return False
    return bool(enr.get("actual_in")) or bool(enr.get("cancelled"))


def should_query(enr: Optional[Dict[str, Any]], leg, now: datetime,
                 adsb_changed: bool, queries_used: int) -> Optional[str]:
    """Why this leg deserves a query right now, or None to skip."""
    if queries_used >= MAX_QUERIES_PER_LEG:
        return None
    if is_finished(enr):
        return None

    dep = leg.dep_datetime_utc()
    if enr is None:
        if dep and now >= dep - PREVIEW_BEFORE_DEP:
            return "first look before departure"
        return None

    last = _parse(enr.get("_fetched_at"))
    if last and (now - last) < MIN_QUERY_GAP:
        return None

    # Something visibly happened on ADS-B — worth confirming against the
    # airline's own record.
    if adsb_changed:
        return "ADS-B state change"

    # No ADS-B to react to (common at outstations with no receiver): fall
    # back to the clock so a leg isn't left un-enriched.
    if dep and not enr.get("actual_out") and now >= dep + SILENT_DEP_FALLBACK:
        return "past scheduled departure with nothing from ADS-B"

    est_on = _parse(enr.get("estimated_on")) or _parse(enr.get("scheduled_on"))
    if est_on and not enr.get("actual_on") and now >= est_on - ARRIVAL_WATCH:
        return "approaching arrival"

    est_in = _parse(enr.get("estimated_in")) or _parse(enr.get("scheduled_in"))
    if est_in and not enr.get("actual_in") and now >= est_in:
        return "arrival due"

    return None


def refresh(user_id: int, leg, now: datetime, adsb_changed: bool = False) -> Optional[str]:
    """Refresh this leg's enrichment if the budget rules allow.

    Returns the reason a query was spent, or None. Never raises — an
    unreachable or rejected API must not disturb tracking, which works
    perfectly well without it.
    """
    api_key = credentials(user_id)
    if not api_key:
        return None

    enr = get_enrichment(user_id, leg.id)
    used = int((enr or {}).get("_queries", 0))
    reason = should_query(enr, leg, now, adsb_changed, used)
    if not reason:
        return None

    try:
        fresh, raw = fetch_leg(api_key, leg.callsign, leg.origin, leg.destination,
                               leg.dep_datetime_utc(), want_raw=True)
    except AeroApiError as e:
        print(f"[enrichment] {leg.id}: {e}")
        # Still counts against the key's usage — the request was made.
        _count_query(user_id, now)
        return None
    except Exception as e:
        print(f"[enrichment] {leg.id}: unexpected error: {e}")
        return None

    _count_query(user_id, now)
    if fresh is None:
        print(f"[enrichment] {leg.id}: no matching flight for {leg.callsign} "
              f"{leg.origin}-{leg.destination}")
        return reason

    fresh["_queries"] = used + 1
    _store(user_id, leg.id, fresh, now, raw=raw)
    print(f"[enrichment] {leg.id}: refreshed ({reason})")
    return reason


# ------------------------------------------------------- status & delay
# How far from schedule still counts as "on time" and stays the normal
# colour. Airlines conventionally use 15 minutes; 5 is tighter, because a
# family member watching wants to know sooner than the DOT does.
ON_TIME_TOLERANCE_MIN = 5


def derive_status(enr: Optional[Dict[str, Any]], adsb_status: Optional[str]) -> Optional[str]:
    """Flight phase, OOOI first, ADS-B as the fallback.

    OOOI leads because it's what actually happened, reported by the
    airline, and it works where there's no ADS-B coverage at all. But
    actual_out in particular is often missing, so where OOOI is silent the
    ADS-B phase machine still answers — the two complement rather than
    compete.
    """
    if not enr:
        return adsb_status
    if enr.get("cancelled"):
        return "Cancelled"
    if enr.get("actual_in"):
        return "Arrived"
    if enr.get("actual_on"):
        return "Taxi-in"
    if enr.get("actual_off"):
        return "Diverting" if enr.get("diverted") else "In Air"
    if enr.get("actual_out"):
        return "Taxi-out"
    # Airborne per ADS-B but OOOI hasn't caught up (actual_out is missing
    # 15-50% of the time), so don't contradict what we can see.
    if adsb_status and adsb_status not in ("Scheduled",):
        return adsb_status
    return adsb_status or "Scheduled"


def _fmt_local(dt: Optional[datetime], tz_name: Optional[str],
               time_format: str = "24") -> Optional[str]:
    """A UTC instant as a clock time at the airport, with its zone."""
    if dt is None or not tz_name:
        return None
    try:
        local = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        return None
    if time_format == "12":
        text = local.strftime("%I:%M %p").lstrip("0")
    else:
        text = local.strftime("%H:%M")
    abbr = local.tzname() or tz_name.split("/")[-1]
    return f"{text} {abbr}"


def _span(minutes: int) -> str:
    amount = abs(minutes)
    if amount >= 60:
        return f"{amount // 60}h {amount % 60:02d}m"
    return f"{amount} min"


def _delay_for(enr, baseline, actual_key, estimate_key, tz_name, time_format,
               verb_future, verb_past, tolerance):
    """Shared shape for the departure and arrival delay blocks.

    `baseline` is the FFDO SCHEDULED time — the pilot's own bid line, not
    the airline's published schedule. Those can differ by a couple of
    minutes, and the pilot flies to the FFDO, so that's what "late" is
    measured against here.
    """
    if not enr or baseline is None:
        return None
    actual = _parse(enr.get(actual_key))
    estimate = _parse(enr.get(estimate_key))
    compare = actual or estimate
    if compare is None:
        return None

    # Truncate BOTH to the minute before doing anything else. The displayed
    # clock time drops seconds, so if the delta were computed from the raw
    # instants it could round the other way and leave the note disagreeing
    # with the time printed next to it by a minute — exactly the mismatch
    # this whole block exists to fix.
    compare = compare.replace(second=0, microsecond=0)
    baseline = baseline.replace(second=0, microsecond=0)

    minutes = int(round((compare - baseline).total_seconds() / 60))
    settled = actual is not None
    revised = _fmt_local(compare, tz_name, time_format)
    original = _fmt_local(baseline, tz_name, time_format)

    if abs(minutes) <= tolerance:
        return {
            "state": "ontime", "minutes": minutes, "settled": settled,
            "time": revised, "original": original,
            "text": f"{verb_past} on time" if settled else "On time",
        }

    word = "late" if minutes > 0 else "early"
    verb = verb_past if settled else verb_future
    return {
        "state": "late" if minutes > 0 else "early",
        "minutes": minutes,
        "settled": settled,
        "time": revised,
        # Only worth showing the original when it actually differs.
        "original": original,
        "text": f"{verb} {_span(minutes)} {word}",
    }


def departure_delay(enr, leg, time_format: str = "24", tolerance: int = None):
    """Is he getting out? Measured against the FFDO departure time.

    Separate from arrival on purpose: a maintenance delay at the gate is
    news long before it shows up as a late arrival, and sometimes a late
    departure makes the time up enroute and never becomes one at all.
    """
    tol = ON_TIME_TOLERANCE_MIN if tolerance is None else tolerance
    tz = getattr(leg.origin_info, "timezone", None) if leg.origin_info else None
    return _delay_for(enr, leg.dep_datetime_utc(), "actual_out", "estimated_out",
                      tz, time_format, "Departing", "Departed", tol)


def arrival_delay(enr, leg, time_format: str = "24", tolerance: int = None):
    """When does he get there? Measured against the FFDO arrival time."""
    tol = ON_TIME_TOLERANCE_MIN if tolerance is None else tolerance
    tz = getattr(leg.dest_info, "timezone", None) if leg.dest_info else None
    if enr and enr.get("cancelled"):
        return {"state": "cancelled", "minutes": 0, "settled": True,
                "time": None, "original": None, "text": "Cancelled"}
    return _delay_for(enr, leg.arr_datetime_utc(), "actual_in", "estimated_in",
                      tz, time_format, "Arrives", "Arrived", tol)


def gate_info(enr: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not enr:
        return None
    out = {
        "origin_gate": enr.get("gate_origin"),
        "origin_terminal": enr.get("terminal_origin"),
        "dest_gate": enr.get("gate_destination"),
        "dest_terminal": enr.get("terminal_destination"),
        "baggage": enr.get("baggage_claim"),
    }
    return out if any(out.values()) else None


def diversion_info(enr: Optional[Dict[str, Any]], scheduled_dest: str) -> Optional[Dict[str, Any]]:
    """Where it's actually going, if that stopped being the plan."""
    if not enr or not enr.get("diverted"):
        return None
    actual_dest = enr.get("destination")
    if actual_dest and actual_dest != (scheduled_dest or "").upper():
        return {"diverted_to": actual_dest, "scheduled": scheduled_dest}
    return {"diverted_to": None, "scheduled": scheduled_dest}
