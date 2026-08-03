"""Background track recorder.

Until v2.6, positions were only ever written while somebody had the
tracker page open, because record_position() was reachable only from a
request handler. That meant the pilot — the one person guaranteed NOT to
be watching — got no track for his own flights, and "Arrived" could never
be detected on an unwatched flight, since the phase machine reads the same
recorded history.

This runs a single background thread that finds every flight currently
inside its scheduled window, across all accounts, and records its position
whether or not anyone is looking.

Deliberately narrow: it records lat/lon (plus ground state, which the
phase machine needs) and nothing else. It does not compute status, touch
schedules, or write anywhere except flight_tracks.

Rate limiting is NOT handled here — livesource.live_state() owns the
shared cache and the 1 req/sec floor, so the poller and any live viewers
naturally share one upstream request per flight instead of competing.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Dict, List
from zoneinfo import ZoneInfo

from .db import get_connection
from .livesource import live_state
from .schedule import get_current_info
from .track import record_position

# How often to sweep for active flights. Matches the viewer's default poll
# so a recorded track has the same resolution whether or not anyone was
# watching at the time.
INTERVAL_S = int(os.environ.get("TRACK_POLLER_INTERVAL_S", "20"))

# Escape hatch: set TRACK_POLLER_ENABLED=0 to run request-driven only.
ENABLED = os.environ.get("TRACK_POLLER_ENABLED", "1") not in ("0", "false", "False")

_thread: threading.Thread | None = None
_started = False
_lock = threading.Lock()


def _all_user_ids() -> List[int]:
    conn = get_connection()
    try:
        return [r["id"] for r in conn.execute("SELECT id FROM users ORDER BY id")]
    finally:
        conn.close()


def active_flights() -> Dict[str, str]:
    """Every flight currently in its window, as {leg_id: callsign}.

    Deduplicated across accounts: if several pilots share a flight it is
    polled once, and because tracks are keyed by flight rather than user,
    that single fetch serves all of them.
    """
    found: Dict[str, str] = {}
    for user_id in _all_user_ids():
        try:
            info = get_current_info(user_id)
        except Exception as e:
            print(f"[poller] schedule lookup failed for user {user_id}: {e}")
            continue
        leg = info.current
        if leg and leg.callsign:
            found[leg.id] = leg.callsign
    return found


def poll_once() -> int:
    """One sweep. Returns how many flights had a position recorded."""
    recorded = 0
    now = datetime.now(ZoneInfo("UTC"))
    for leg_id, callsign in active_flights().items():
        try:
            state = live_state(callsign)
        except Exception as e:
            print(f"[poller] lookup failed for {callsign}: {e}")
            continue
        if not state or state.get("lat") is None or state.get("lon") is None:
            continue
        try:
            record_position(leg_id, state["lat"], state["lon"], now, state.get("on_ground"))
            recorded += 1
        except Exception as e:
            print(f"[poller] record failed for {leg_id}: {e}")
    return recorded


def _loop() -> None:
    print(f"[poller] track recorder running every {INTERVAL_S}s")
    while True:
        try:
            poll_once()
        except Exception as e:
            # Never let one bad sweep kill the thread — a poller that dies
            # silently would look exactly like the bug this replaced.
            print(f"[poller] sweep failed: {e}")
        time.sleep(INTERVAL_S)


def start() -> bool:
    """Start the recorder once per process. Safe to call repeatedly."""
    global _thread, _started
    if not ENABLED:
        print("[poller] disabled via TRACK_POLLER_ENABLED=0")
        return False
    with _lock:
        if _started:
            return False
        _thread = threading.Thread(target=_loop, name="track-poller", daemon=True)
        _thread.start()
        _started = True
        return True
