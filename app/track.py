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

LANDING_RADIUS_NM = 8.0        # In Air -> Landing inside this range. 17 nm fired while
                               # still being vectored downwind; 8 is about final.
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


def compute_phase(leg: FlightLeg, live: Optional[Dict[str, Any]],
                  history: List[Dict[str, Any]], now: datetime,
                  poll_seconds: int) -> str:
    """Flight phase, from what the aircraft is actually broadcasting.

    This deliberately does NOT guess. Earlier versions inferred a phase
    from the clock and from silence: scheduled departure had passed so the
    flight was "Departing"; the signal dropped while airborne so it "must
    still be flying"; the signal dropped on the ground so it "must have
    arrived". Air travel doesn't cooperate with that — gate holds, returns
    to stand and diversions all break it, and a coverage gap isn't
    evidence of anything.

    So: if the aircraft is being tracked, the phase comes from the data.
    If it isn't, the answer is Unknown, and the card says when it was last
    seen. We pick back up wherever the aircraft reappears.

    "Arrived" is NOT produced here. That's a closure decision (closure.py),
    which requires the aircraft stopped AND the signal gone, or the
    airline's own gate-in. "Diverted" and "Cancelled" come only from the
    API — ADS-B has no concept of either.
    """
    dep = leg.dep_datetime_utc()

    tracked = live and live.get("lat") is not None and live.get("lon") is not None
    if tracked:
        if not live.get("on_ground"):
            dest = leg.dest_info
            if dest and dest.lat is not None:
                d = haversine_nm(live["lat"], live["lon"], dest.lat, dest.lon)
                if d <= LANDING_RADIUS_NM:
                    return "Landing"
            return "In Air"

        # On the ground. Which side of the flight depends on whether we
        # have actually seen it airborne — an observed fact, not a guess.
        ever_airborne = any(h.get("on_ground") is False for h in history)
        if ever_airborne:
            return "Taxi-in"
        start = history[0] if history else None
        if start and start.get("lat") is not None:
            moved = haversine_nm(live["lat"], live["lon"], start["lat"], start["lon"])
            if moved > GROUND_MOVE_THRESHOLD_NM:
                return "Taxi-out"
        return "Scheduled"

    # Not being tracked. Before departure that's simply the schedule
    # speaking, which is honest. Afterwards we genuinely don't know.
    if dep and now < dep:
        return "Scheduled"
    return "Unknown"


def last_tracked_at(history: List[Dict[str, Any]]) -> Optional[datetime]:
    """When the aircraft was last seen, for the Unknown state."""
    if not history:
        return None
    last = history[-1]
    return last.get("ts")


def compute_progress(leg: FlightLeg, live: Optional[Dict[str, Any]], now: datetime,
                     departed: Optional[bool] = None,
                     dep_override: Optional[datetime] = None,
                     arr_override: Optional[datetime] = None) -> Optional[float]:
    """Percent (0-100) along the route.

    Great-circle position when there's a live fix; otherwise elapsed time
    against the flight's duration.

    Two guards on that fallback, both learned the hard way:

    `departed=False` pins progress at zero. Without it, a flight delayed at
    the gate showed "27.7% en route" simply because its SCHEDULED departure
    had passed — the clock had moved even though the aeroplane hadn't.

    `dep_override`/`arr_override` let the caller pass revised times, so a
    known delay shifts the whole calculation instead of measuring against a
    schedule everyone already knows is wrong.
    """
    if departed is False:
        return 0.0
    dep = dep_override or leg.dep_datetime_utc()
    arr = arr_override or leg.arr_datetime_utc()
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

    # No live position means no progress figure. The old elapsed-time
    # fallback measured the clock rather than the aeroplane, which is how a
    # flight still at the gate reported 27% (and later 100%) en route.
    # Better to show nothing than something invented.
    return None


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


def compute_remaining_minutes(leg: FlightLeg, live: Optional[Dict[str, Any]], now: datetime,
                              arr_override: Optional[datetime] = None) -> Optional[float]:
    """Minutes until arrival, from live groundspeed and distance-to-go when
    available, otherwise scheduled arrival minus now. Shared by both the
    "time remaining" and "arriving at" displays so they can never disagree.
    """
    dest_info = leg.dest_info
    arr_utc = arr_override or leg.arr_datetime_utc()

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
    # Only when the caller supplied a REVISED arrival (the airline's
    # estimate). The bare schedule isn't knowledge — a flight two hours
    # late doesn't have 5 minutes to go just because the timetable says so.
    if arr_override:
        return max(0.0, (arr_override - now).total_seconds() / 60)
    return None


def compute_ete(leg: FlightLeg, live: Optional[Dict[str, Any]], now: datetime,
                arr_override: Optional[datetime] = None) -> Optional[str]:
    """Time remaining (ETE), not a clock time. See compute_remaining_minutes."""
    remaining_minutes = compute_remaining_minutes(leg, live, now, arr_override)
    if remaining_minutes is None:
        return None
    total_minutes = int(round(max(0, remaining_minutes)))
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes} min"
