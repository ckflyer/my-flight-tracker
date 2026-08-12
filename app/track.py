"""The breadcrumb trail, and the progress / distance / ETE maths.

Tracks are PERSISTENT and keyed by FLIGHT, not by user: a path is a fact
about an aeroplane, not about a person. Two crew on ENY3729 share one
path, and a deadhead leg and a working leg on the same flight record into
the same trail rather than splitting into two half-recorded ones.

Points are thinned on write — a fix is skipped unless the aircraft moved
at least MIN_POINT_SEPARATION_NM from the last stored one — so a parked
aeroplane stores one row instead of one per poll. Ground-state changes are
always kept, because takeoff and landing are read off them.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .db import get_connection
from .flights import flight_key
from .geo import haversine_nm
from .models import FlightLeg

# Per-leg safety valve. At a 20s poll a 4-hour flight is ~720 raw fixes
# and thinning drops most of the stationary ones, so this sits far above
# anything real — it exists only so a stuck poller can't grow one leg
# without bound. The old value of 300 silently ate the START of any flight
# over ~75 minutes, since the cap discards oldest-first.
MAX_TRACK_POINTS = 3000

# Kills the long runs of near-identical fixes from a parked aircraft (and
# from the shared cache handing the same fix to several polls), which
# would otherwise render as a blob at the gate.
MIN_POINT_SEPARATION_NM = 0.12

TRACK_RETENTION_DAYS = 30
MIN_SPEED_KTS_FOR_ETA = 20  # ignore near-zero groundspeed so ETE can't spike

_prune_lock = threading.Lock()
_last_prune_at: float = 0.0
PRUNE_INTERVAL_S = 3600.0


def prune_old_positions(retention_days: int = TRACK_RETENTION_DAYS) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM positions WHERE ts < ?", (cutoff,))
        removed = cur.rowcount or 0
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
            print(f"[track] pruned {removed} points older than {TRACK_RETENTION_DAYS} days")
    except Exception as e:
        print(f"[track] prune failed: {e}")


def record_position(leg_id: str, lat: float, lon: float, ts: datetime,
                    on_ground: Optional[bool] = None) -> None:
    """Append a position to this FLIGHT's track."""
    key = flight_key(leg_id)
    conn = get_connection()
    try:
        last = conn.execute(
            "SELECT lat, lon, on_ground FROM positions WHERE flight_key = ? "
            "ORDER BY ts DESC LIMIT 1", (key,)
        ).fetchone()
        if last is not None:
            last_ground = None if last["on_ground"] is None else bool(last["on_ground"])
            if (last_ground == on_ground
                    and haversine_nm(last["lat"], last["lon"], lat, lon)
                    < MIN_POINT_SEPARATION_NM):
                return
        conn.execute(
            "INSERT OR IGNORE INTO positions (flight_key, ts, lat, lon, on_ground) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, ts.isoformat(), lat, lon,
             None if on_ground is None else int(on_ground)),
        )
        conn.execute(
            "DELETE FROM positions WHERE flight_key = ? AND rowid NOT IN "
            "(SELECT rowid FROM positions WHERE flight_key = ? ORDER BY ts DESC LIMIT ?)",
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
            "SELECT lat, lon FROM positions WHERE flight_key = ? ORDER BY ts ASC",
            (flight_key(leg_id),),
        ).fetchall()
    finally:
        conn.close()
    return [[r["lat"], r["lon"]] for r in rows]


def get_position_history(leg_id: str) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT ts, lat, lon, on_ground FROM positions WHERE flight_key = ? "
            "ORDER BY ts ASC", (flight_key(leg_id),),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["ts"])
        except Exception:
            continue
        out.append({"ts": ts, "lat": r["lat"], "lon": r["lon"],
                    "on_ground": None if r["on_ground"] is None else bool(r["on_ground"])})
    return out


# ------------------------------------------------------------ the maths
def compute_progress(leg: FlightLeg, lat: Optional[float], lon: Optional[float],
                     departed: bool) -> Optional[float]:
    """Percent along the great-circle route, or None.

    EVERY figure on the route strip comes from a live position fix or does
    not exist. The old elapsed-time fallback measured the clock against the
    schedule, so a flight still at the gate showed 27% en route and, once
    past its scheduled arrival, 100%.

    Through v5.6 this returned 0.0 for a leg that had not departed, which
    was a smaller version of the same lie: a pinned zero looks like a
    measurement and is not one. It also could not stay honest once the
    figure moved onto the always-visible card, where a parked aeroplane
    icon sitting at the origin reads as "we are tracking this" rather than
    "it has not gone anywhere yet". None now means None: no fix, no figure,
    nothing drawn.

    The `departed` gate stays, and is protective rather than cosmetic. The
    hex lock can point at an airframe still inbound on ITS previous leg, so
    a fix taken before this leg pushes back can belong to an aeroplane
    halfway across the state.
    """
    if not departed:
        return None
    if lat is None or lon is None:
        return None
    o, d = leg.origin_info, leg.dest_info
    if not o or not d or o.lat is None or d.lat is None:
        return None
    total = haversine_nm(o.lat, o.lon, d.lat, d.lon)
    if total <= 0:
        return None
    done = haversine_nm(o.lat, o.lon, lat, lon)
    return round(max(0.0, min(100.0, done / total * 100)), 1)


def compute_distance_nm(leg: FlightLeg, lat: Optional[float],
                        lon: Optional[float]) -> Optional[float]:
    d = leg.dest_info
    if not d or d.lat is None or lat is None or lon is None:
        return None
    return round(haversine_nm(lat, lon, d.lat, d.lon), 1)


def compute_remaining_minutes(leg: FlightLeg, lat, lon,
                              speed_kts) -> Optional[float]:
    """Minutes to go, from live groundspeed and distance. Otherwise None.

    Through v5.6 this fell back to counting down to the airline's revised
    arrival when there was no fix or the aircraft was below taxi speed.
    That figure is clock arithmetic wearing the same clothes as a measured
    one, and it failed in the worst place: on a coverage hole mid-cruise
    the percentage and the distance both correctly vanished while "ETE
    21 min" stayed lit beside them, ticking down on a timetable. One
    number on the card contradicting the two next to it is worse than
    three blanks.

    The revised arrival is still shown — it is the Arrival row, where it
    is labelled as the airline's estimate and belongs.
    """
    d = leg.dest_info
    if (lat is not None and lon is not None and speed_kts
            and speed_kts > MIN_SPEED_KTS_FOR_ETA and d and d.lat is not None):
        remaining = haversine_nm(lat, lon, d.lat, d.lon)
        return max(0.0, (remaining / speed_kts) * 60)
    return None


def format_ete(minutes: Optional[float]) -> Optional[str]:
    if minutes is None:
        return None
    total = int(round(max(0, minutes)))
    h, m = total // 60, total % 60
    return f"{h}h {m:02d}m" if h else f"{m} min"
