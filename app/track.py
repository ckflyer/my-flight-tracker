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

LANDING_RADIUS_NM = 17.0       # switch In Air -> Landing within this range of the destination
GROUND_MOVE_THRESHOLD_NM = 0.1  # ~600ft; cumulative displacement counts as "moved," not instantaneous speed
STILL_WINDOW_SECONDS = 240      # how long position must stay put before calling it Arrived (rides out taxi-queue stops)
MIN_SIGNAL_LOST_SECONDS = 90    # how long ADS-B can go quiet after a landing before we just call it Arrived


def record_position(leg_id: str, lat: float, lon: float, ts: datetime, on_ground: Optional[bool] = None) -> None:
    """Append a live position for the current leg. Clears breadcrumbs from any
    other leg so the table stays small and always reflects the active flight."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM positions WHERE leg_id != ?", (leg_id,))
        conn.execute(
            "INSERT INTO positions (leg_id, ts, lat, lon, on_ground) VALUES (?, ?, ?, ?, ?)",
            (leg_id, ts.isoformat(), lat, lon, None if on_ground is None else int(on_ground)),
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


def get_position_history(leg_id: str) -> List[Dict[str, Any]]:
    """Chronological position history for this leg, including ground/air state.
    Used for phase detection (taxi-out/in, arrived) — not just map drawing."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT ts, lat, lon, on_ground FROM positions WHERE leg_id = ? ORDER BY ts ASC",
            (leg_id,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["ts"])
        except Exception:
            continue
        out.append({
            "ts": ts,
            "lat": r["lat"],
            "lon": r["lon"],
            "on_ground": None if r["on_ground"] is None else bool(r["on_ground"]),
        })
    return out


def _stable_since(history: List[Dict[str, Any]], anchor_lat: float, anchor_lon: float, now: datetime, threshold_nm: float) -> datetime:
    """Walk backward through history from most-recent to oldest, and return
    the earliest timestamp such that every point from there to now stayed
    within threshold_nm of the current (anchor) position. This treats
    "stopped for good" as sustained closeness over time, not instantaneous
    speed — so a stop-and-go taxi queue doesn't look identical to parking
    at the gate."""
    stable_from = now
    for p in reversed(history):
        if p.get("lat") is None:
            continue
        d = haversine_nm(anchor_lat, anchor_lon, p["lat"], p["lon"])
        if d > threshold_nm:
            break
        stable_from = p["ts"]
    return stable_from


def compute_phase(leg: FlightLeg, live: Optional[Dict[str, Any]], history: List[Dict[str, Any]], now: datetime, poll_seconds: int = 45) -> str:
    """Live-data-driven flight phase: Scheduled / Departing / Taxi-out /
    In Air / Landing / Taxi-in / Arrived.

    Ground movement (taxi-out/in) is judged by cumulative displacement from
    a fixed reference point, not instantaneous speed — a plane stopped in a
    taxi queue still reads as "moving" in this sense, since it's already
    displaced from where it started. "Arrived" only fires once position has
    been stable for a sustained window (rides out a queue stop) or ADS-B
    has gone quiet for a bit after a landing was already confirmed (common
    at fields with weak ramp-area coverage) — never from silence before
    we've confirmed anything actually happened.
    """
    dep = leg.dep_datetime_utc()
    arr = leg.arr_datetime_utc()
    if not dep or not arr:
        return "Unknown"

    if now < dep - timedelta(minutes=20):
        return "Scheduled"

    ever_airborne = any(p["on_ground"] is False for p in history)
    dest = leg.dest_info

    if live and live.get("lat") is not None and live.get("lon") is not None:
        on_ground_now = bool(live.get("on_ground"))
        if not on_ground_now:
            if dest and dest.lat is not None:
                d = haversine_nm(live["lat"], live["lon"], dest.lat, dest.lon)
                if d <= LANDING_RADIUS_NM:
                    return "Landing"
            return "In Air"

        # On the ground right now.
        if not ever_airborne:
            start = history[0] if history else None
            if start and start.get("lat") is not None:
                moved = haversine_nm(live["lat"], live["lon"], start["lat"], start["lon"])
                if moved > GROUND_MOVE_THRESHOLD_NM:
                    return "Taxi-out"
            return "Departing"

        stable_from = _stable_since(history, live["lat"], live["lon"], now, GROUND_MOVE_THRESHOLD_NM)
        if (now - stable_from).total_seconds() >= STILL_WINDOW_SECONDS:
            return "Arrived"
        return "Taxi-in"

    # No live signal this poll.
    if not ever_airborne:
        return "Departing"
    last = history[-1] if history else None
    if last and last.get("on_ground"):
        elapsed = (now - last["ts"]).total_seconds()
        if elapsed >= max(poll_seconds * 2, MIN_SIGNAL_LOST_SECONDS):
            return "Arrived"
        return "Taxi-in"
    # Last known state was airborne but the signal dropped — report the
    # last confirmed state rather than guessing.
    return "In Air"


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
