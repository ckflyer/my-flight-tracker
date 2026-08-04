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
from .livesource import live_state_for_leg
from .schedule import get_current_info
from .track import record_position, get_position_history
from .enrichment import refresh as refresh_enrichment

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
    """Every flight currently in its window, as {leg_id: leg}.

    Deduplicated across accounts: if several pilots share a flight it is
    polled once, and because tracks are keyed by flight rather than user,
    that single fetch serves all of them.

    Returns {leg_id: FlightLeg} — the leg itself, not just its callsign,
    because verifying an aircraft actually belongs to this leg needs the
    origin and destination.
    """
    found: Dict[str, object] = {}
    for user_id in _all_user_ids():
        try:
            info = get_current_info(user_id)
        except Exception as e:
            print(f"[poller] schedule lookup failed for user {user_id}: {e}")
            continue
        leg = info.current
        if leg and leg.callsign:
            found[leg.id] = leg
    return found


def _owners_of(leg_id: str) -> List[int]:
    """Which accounts have this leg — i.e. whose AeroAPI key may pay."""
    conn = get_connection()
    try:
        return [r["user_id"] for r in conn.execute(
            "SELECT DISTINCT user_id FROM legs WHERE id = ?", (leg_id,))]
    finally:
        conn.close()


def poll_once() -> int:
    """One sweep. Returns how many flights had a position recorded."""
    recorded = 0
    now = datetime.now(ZoneInfo("UTC"))
    for leg_id, leg in active_flights().items():
        try:
            # Same guard the page uses: a callsign match that's heading the
            # wrong way is the return flight, and recording it here would
            # silently corrupt the stored track with nobody watching.
            history = get_position_history(leg_id)
            state = live_state_for_leg(leg, now, history)
        except Exception as e:
            print(f"[poller] lookup failed for {leg.callsign}: {e}")
            continue
        if not state or state.get("lat") is None or state.get("lon") is None:
            # No ADS-B for this leg — precisely when the airline's own OOOI
            # matters most, so still give enrichment a chance.
            for user_id in _owners_of(leg_id):
                try:
                    refresh_enrichment(user_id, leg, now, adsb_changed=False)
                except Exception as e:
                    print(f"[poller] enrichment failed for {leg_id}/{user_id}: {e}")
            continue
        try:
            before = len(history)
            record_position(leg_id, state["lat"], state["lon"], now, state.get("on_ground"))
            recorded += 1
            # A stored point means the aircraft actually moved or changed
            # ground state — the cheap signal that something happened, and
            # the primary trigger for spending an AeroAPI query.
            changed = len(get_position_history(leg_id)) > before
        except Exception as e:
            print(f"[poller] record failed for {leg_id}: {e}")
            changed = False

        for user_id in _owners_of(leg_id):
            try:
                refresh_enrichment(user_id, leg, now, adsb_changed=changed)
            except Exception as e:
                print(f"[poller] enrichment failed for {leg_id}/{user_id}: {e}")
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
