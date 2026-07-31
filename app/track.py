"""Breadcrumb position history + flight-progress / ETA math for the current leg."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .db import get_connection
from .geo import haversine_nm
from .models import FlightLeg

MAX_BREADCRUMB_POINTS = 300
MIN_SPEED_KTS_FOR_ETA = 20  # ignore near-zero ground speed so ETA doesn't spike


def record_position(leg_id: str, lat: float, lon: float, ts: datetime) -> None:
    """Append a live position for the current leg. Clears breadcrumbs from any
    other leg so the table stays small and always reflects the active flight."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM positions WHERE leg_id != ?", (leg_id,))
        conn.execute(
            "INSERT INTO positions (leg_id, ts, lat, lon) VALUES (?, ?, ?, ?)",
            (leg_id, ts.isoformat(), lat, lon),
        )
        conn.execute(
            """
            DELETE FROM positions WHERE leg_id = ? AND rowid NOT IN (
                SELECT rowid FROM positions WHERE leg_id = ? ORDER BY ts DESC LIMIT ?
            )
            """,
            (leg_id, leg_id, MAX_BREADCRUMB_POINTS),
        )
        conn.commit()
    finally:
        conn.close()


def get_breadcrumb(leg_id: str) -> List[List[float]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT lat, lon FROM positions WHERE leg_id = ? ORDER BY ts ASC",
            (leg_id,),
        ).fetchall()
    finally:
        conn.close()
    return [[r["lat"], r["lon"]] for r in rows]


def compute_progress(leg: FlightLeg, live: Optional[Dict[str, Any]], now: datetime) -> Optional[float]:
    """Percent (0-100) along the route. Uses great-circle position when we
    have a live lat/lon and both airports' coordinates; otherwise falls back
    to elapsed-time-vs-scheduled-duration."""
    dep = leg.dep_datetime_utc()
    arr = leg.arr_datetime_utc()
    if not dep or not arr or arr <= dep:
        return None

    if (
        live
        and live.get("lat") is not None
        and live.get("lon") is not None
        and leg.origin_info
        and leg.dest_info
        and leg.origin_info.lat is not None
        and leg.dest_info.lat is not None
    ):
        total = haversine_nm(leg.origin_info.lat, leg.origin_info.lon, leg.dest_info.lat, leg.dest_info.lon)
        done = haversine_nm(leg.origin_info.lat, leg.origin_info.lon, live["lat"], live["lon"])
        if total > 0:
            return round(max(0.0, min(100.0, done / total * 100)), 1)

    pct = (now - dep).total_seconds() / (arr - dep).total_seconds() * 100
    return round(max(0.0, min(100.0, pct)), 1)


def compute_eta(leg: FlightLeg, live: Optional[Dict[str, Any]], now: datetime, time_format: str = "24") -> Optional[str]:
    """Local-time-at-destination ETA string. Uses live position + groundspeed
    when available, otherwise falls back to the scheduled arrival time."""
    dest_info = leg.dest_info
    if not dest_info:
        return None

    eta_utc = leg.arr_datetime_utc()

    if (
        live
        and live.get("lat") is not None
        and live.get("lon") is not None
        and live.get("speed_kts")
        and live["speed_kts"] > MIN_SPEED_KTS_FOR_ETA
        and dest_info.lat is not None
    ):
        remaining_nm = haversine_nm(live["lat"], live["lon"], dest_info.lat, dest_info.lon)
        hours = remaining_nm / live["speed_kts"]
        eta_utc = now + timedelta(hours=hours)

    if not eta_utc:
        return None

    tz = ZoneInfo(dest_info.timezone)
    local = eta_utc.astimezone(tz)
    if time_format == "12":
        return local.strftime("%I:%M %p").lstrip("0")
    return local.strftime("%H:%M")
