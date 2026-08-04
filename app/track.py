"""Position history + flight-progress / ETA math.

Tracks are PERSISTENT: each leg keeps its own flown path so a completed
flight can be replayed on the map later. Until v2.2 every write deleted
every other leg's points, so only the in-progress flight ever had a track.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

from .db import get_connection
from .geo import haversine_nm
from .models import FlightLeg

# Per-leg safety valve. At a 15s poll a 4-hour flight is ~960 raw fixes and
# thinning drops most of the stationary ones, so this sits far above
# anything real — it exists only so a stuck poller can't grow one leg
# without bound. The old value of 300 would have silently eaten the START
# of any flight over ~75 minutes, since the cap discards oldest-first.
MAX_TRACK_POINTS = 3000

# Don't store a new point unless the aircraft moved at least this far from
# the last stored one. Kills the long runs of near-identical fixes from a
# parked aircraft (and from the shared cache handing the same fix to
# several polls), which would otherwise render as a blob at the gate.
MIN_POINT_SEPARATION_NM = 0.12

# How long completed flight tracks are kept before being pruned.
TRACK_RETENTION_DAYS = 30

MIN_SPEED_KTS_FOR_ETA = 20  # ignore near-zero ground speed so ETA doesn't spike

LANDING_RADIUS_NM = 17.0       # switch In Air -> Landing within this range of the destination
GROUND_MOVE_THRESHOLD_NM = 0.1  # ~600ft; cumulative displacement counts as "moved," not instantaneous speed
STILL_WINDOW_SECONDS = 240      # how long position must stay put before calling it Arrived (rides out taxi-queue stops)
MIN_SIGNAL_LOST_SECONDS = 90    # how long ADS-B can go quiet after a landing before we just call it Arrived

_prune_lock = threading.Lock()
_last_prune_at: float = 0.0
PRUNE_INTERVAL_S = 3600.0


def flight_key(leg_id: str) -> str:
    """Shared identity of a physical flight.

    Leg ids look like "2026-08-04-3729-DFW-OKC", with "-DH" appended when
    the pilot is deadheading. That suffix describes the PERSON's role, not
    the aeroplane, so it's stripped here — otherwise a deadhead and a
    working leg on the same flight would record two separate half-tracks.
    """
    if leg_id.endswith("-DH"):
        return leg_id[:-3]
    return leg_id


def prune_old_positions(retention_days: int = TRACK_RETENTION_DAYS) -> int:
    """Delete stored track points older than the retention window.

    Returns the number of rows removed. Cheap (one indexed delete), but
    record_position() throttles it to hourly so it isn't run every poll.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM flight_tracks WHERE ts < ?", (cutoff,))
        removed = cur.rowcount or 0
        # Enrichment ages out with the track it belongs to, so the two stay
        # in step rather than one table growing forever.
        try:
            conn.execute("DELETE FROM flight_enrichment WHERE fetched_at < ?", (cutoff,))
        except Exception:
            pass
        conn.commit()
        return removed
    finally:
        conn.close()


def _maybe_prune() -> None:
    global _last_prune_at
    if time.monotonic() - _last_prune_at < PRUNE_INTERVAL_S:
        return
    with _prune_lock:
        if time.monotonic() - _last_prune_at < PRUNE_INTERVAL_S:
            return
        _last_prune_at = time.monotonic()
    try:
        removed = prune_old_positions()
        if removed:
            print(f"[track] pruned {removed} position rows older than {TRACK_RETENTION_DAYS} days")
    except Exception as e:
        print(f"[track] prune failed: {e}")


def record_position(leg_id: str, lat: float, lon: float, ts: datetime,
                    on_ground: Optional[bool] = None) -> None:
    """Append a position to this FLIGHT's track.

    Not user-scoped: one flight has one path regardless of how many people
    have it on their schedule or are watching it. Callers pass a leg id and
    the flight key is derived here.

    A point is skipped when the aircraft hasn't moved meaningfully since
    the last stored fix AND its ground state is unchanged, so an aircraft
    at a gate doesn't pile up identical rows. Ground-state changes are
    always kept, because the phase machine reads them to detect takeoff
    and landing.
    """
    key = flight_key(leg_id)
    conn = get_connection()
    try:
        last = conn.execute(
            "SELECT lat, lon, on_ground FROM flight_tracks WHERE flight_key = ? "
            "ORDER BY ts DESC LIMIT 1",
            (key,),
        ).fetchone()

        if last is not None:
            last_ground = None if last["on_ground"] is None else bool(last["on_ground"])
            if last_ground == on_ground and \
                    haversine_nm(last["lat"], last["lon"], lat, lon) < MIN_POINT_SEPARATION_NM:
                return

        conn.execute(
            "INSERT OR IGNORE INTO flight_tracks (flight_key, ts, lat, lon, on_ground) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, ts.isoformat(), lat, lon, None if on_ground is None else int(on_ground)),
        )
        conn.execute(
            """
            DELETE FROM flight_tracks WHERE flight_key = ? AND rowid NOT IN (
                SELECT rowid FROM flight_tracks WHERE flight_key = ? ORDER BY ts DESC LIMIT ?
            )
            """,
            (key, key, MAX_TRACK_POINTS),
        )
        conn.commit()
    finally:
        conn.close()
    _maybe_prune()


def get_breadcrumb(leg_id: str) -> List[List[float]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT lat, lon FROM flight_tracks WHERE flight_key = ? ORDER BY ts ASC",
            (flight_key(leg_id),),
        ).fetchall()
    finally:
        conn.close()
    return [[r["lat"], r["lon"]] for r in rows]


def get_position_history(leg_id: str) -> List[Dict[str, Any]]:
    """Chronological position history for this flight, including ground/air
    state. Used for phase detection (taxi-out/in, arrived), not just map
    drawing — which is why the background poller matters: without recorded
    history, "Arrived" can never be detected."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT ts, lat, lon, on_ground FROM flight_tracks WHERE flight_key = ? ORDER BY ts ASC",
            (flight_key(leg_id),),
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


def compute_distance_remaining_nm(leg: FlightLeg, live: Optional[Dict[str, Any]]) -> Optional[float]:
    """Straight-line distance from the current live position to the
    destination. Only meaningful with an actual live position — there's no
    sensible non-live fallback for "how far away is it right now"."""
    dest_info = leg.dest_info
    if not dest_info or dest_info.lat is None:
        return None
    if not live or live.get("lat") is None or live.get("lon") is None:
        return None
    return round(haversine_nm(live["lat"], live["lon"], dest_info.lat, dest_info.lon), 1)


def compute_remaining_minutes(leg: FlightLeg, live: Optional[Dict[str, Any]], now: datetime) -> Optional[float]:
    """Minutes until arrival, from live groundspeed and distance-to-go when
    available, otherwise scheduled arrival minus now. Shared by both the
    "time remaining" and "arriving at" displays so they can never disagree.
    """
    dest_info = leg.dest_info
    arr_utc = leg.arr_datetime_utc()

    if (
        live
        and live.get("lat") is not None
        and live.get("lon") is not None
        and live.get("speed_kts")
        and live["speed_kts"] > MIN_SPEED_KTS_FOR_ETA
        and dest_info and dest_info.lat is not None
    ):
        remaining_nm = haversine_nm(live["lat"], live["lon"], dest_info.lat, dest_info.lon)
        return max(0.0, (remaining_nm / live["speed_kts"]) * 60)
    if arr_utc:
        return max(0.0, (arr_utc - now).total_seconds() / 60)
    return None


def compute_eta(leg: FlightLeg, live: Optional[Dict[str, Any]], now: datetime,
                time_format: str = "24") -> Optional[str]:
    """Predicted arrival as a CLOCK TIME in the destination's local zone.

    "Landing about 6:42 PM" is the thing a family member actually wants;
    "1h 12m" makes them do arithmetic against a wall clock. Destination
    local time matches the scheduled arrival already shown on the card, so
    the two can be compared directly to see if a flight is running late.
    """
    remaining = compute_remaining_minutes(leg, live, now)
    if remaining is None:
        return None
    dest_info = leg.dest_info
    if not dest_info:
        return None
    eta_utc = now + timedelta(minutes=remaining)
    try:
        local = eta_utc.astimezone(ZoneInfo(dest_info.timezone))
    except Exception:
        return None
    if time_format == "12":
        time_str = local.strftime("%I:%M %p").lstrip("0")
    else:
        time_str = local.strftime("%H:%M")
    abbr = local.tzname() or dest_info.timezone.split("/")[-1]
    return f"{time_str} {abbr}"


def compute_ete(leg: FlightLeg, live: Optional[Dict[str, Any]], now: datetime) -> Optional[str]:
    """Time remaining (ETE), not a clock time. See compute_remaining_minutes."""
    remaining_minutes = compute_remaining_minutes(leg, live, now)
    if remaining_minutes is None:
        return None
    total_minutes = int(round(max(0, remaining_minutes)))
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes} min"
