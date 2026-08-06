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
from datetime import datetime, timedelta
from typing import Dict, List
from zoneinfo import ZoneInfo

from .db import get_connection
from .livesource import live_state_for_leg
from .schedule import get_current_info
from .track import record_position, get_position_history
from .enrichment import (refresh as refresh_enrichment, credentials,
                         get_enrichment, refresh_usage, budget_state)
from .carrier import resolve as resolve_carrier, needs_resolution
from .closure import maybe_close, is_closed
from .flightmatch import (observed_gate_in, took_off_again,
                          signal_gap_seconds, stopped_seconds, landed_at,
                          has_flown, took_off_at)

# How often to sweep for active flights. Matches the viewer's default poll
# so a recorded track has the same resolution whether or not anyone was
# watching at the time.
INTERVAL_S = int(os.environ.get("TRACK_POLLER_INTERVAL_S", "20"))

# How early a not-yet-current leg gets swept. Five minutes wider than
# enrichment's PREVIEW_BEFORE_DEP (T-30) so the first look is reachable
# with margin rather than depending on a sweep landing exactly on it.
PREVIEW_WINDOW = timedelta(minutes=35)

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
    now = datetime.now(ZoneInfo("UTC"))
    for user_id in _all_user_ids():
        try:
            info = get_current_info(user_id)
        except Exception as e:
            print(f"[poller] schedule lookup failed for user {user_id}: {e}")
            continue
        leg = info.current
        if leg and leg.callsign:
            found[leg.id] = leg
        # A leg doesn't become info.current until T-20, but the query
        # schedule wants its first look at T-30 — so that branch was
        # unreachable and no gate, revised arrival, or published delay ever
        # arrived before pushback. By T-30 the flight plan is filed and all
        # three exist, which is exactly when they're worth having.
        #
        # Widened here rather than in get_current_info on purpose: "current"
        # drives flight selection, the map, and the card everywhere else,
        # and moving that boundary would change all of it as a side effect.
        for leg in info.upcoming:
            dep = leg.dep_datetime_utc()
            if leg.callsign and dep and now >= dep - PREVIEW_WINDOW:
                found.setdefault(leg.id, leg)
    return found


def _owners_of(leg_id: str) -> List[int]:
    """Which accounts have this leg — i.e. whose AeroAPI key may pay."""
    conn = get_connection()
    try:
        return [r["user_id"] for r in conn.execute(
            "SELECT DISTINCT user_id FROM legs WHERE id = ?", (leg_id,))]
    finally:
        conn.close()


def _settle(leg_id: str, leg, now, has_adsb: bool) -> None:
    """Refresh enrichment and decide whether the leg is finished.

    Runs for every owner of the leg, since each pilot's own key pays for
    their own enrichment and each keeps their own closeout record.
    """
    observed_in = observed_gate_in(leg)
    relaunched = took_off_again(leg)
    stopped_for = stopped_seconds(leg, now)
    signal_gap = signal_gap_seconds(leg, now)
    touchdown = landed_at(leg)
    departed = has_flown(leg)
    wheels_up = took_off_at(leg)
    # "Down" gates the closeout pass: only worth hunting for gate-in once
    # the aircraft has actually stopped somewhere.
    down = observed_in is not None
    for user_id in _owners_of(leg_id):
        if is_closed(user_id, leg_id):
            continue
        try:
            refresh_enrichment(user_id, leg, now, down=down, touchdown=touchdown,
                               has_adsb=has_adsb, departed=departed,
                               took_off_at=wheels_up)
        except Exception as e:
            print(f"[poller] enrichment failed for {leg_id}/{user_id}: {e}")
        try:
            # /account/usage is free, so this costs nothing and keeps the
            # spend figure honest. Throttled inside refresh_usage.
            refresh_usage(user_id, now)
        except Exception as e:
            print(f"[poller] usage refresh failed for {user_id}: {e}")
        try:
            enr = get_enrichment(user_id, leg_id)
            # "Fresh" enrichment means the airline is still telling us
            # things, so the flight is still live and the backstop must
            # not fire — however late it is running.
            fresh = False
            if enr and enr.get("_fetched_at"):
                try:
                    from datetime import datetime as _dt, timedelta as _td
                    fresh = (now - _dt.fromisoformat(enr["_fetched_at"])) < _td(minutes=45)
                except Exception:
                    fresh = False
            maybe_close(user_id, leg, enr, now,
                        has_api=bool(credentials(user_id)),
                        observed_in=observed_in, relaunched=relaunched,
                        stopped_for=stopped_for, signal_gap=signal_gap,
                        enrichment_fresh=fresh)
        except Exception as e:
            print(f"[poller] closeout failed for {leg_id}/{user_id}: {e}")


def poll_once() -> int:
    """One sweep. Returns how many flights had a position recorded."""
    recorded = 0
    now = datetime.now(ZoneInfo("UTC"))
    for leg_id, leg in active_flights().items():
        # A deadhead's FFDO line has no carrier, so the callsign has to be
        # resolved before anything is looked up — searching for ENY4110
        # when the aircraft squawks AAL4110 finds nothing. Resolved once
        # and stored on the leg, not repeated every sweep.
        if needs_resolution(leg):
            # Pick an owner whose key is BOTH present and still inside its
            # budget. Carrier resolution costs a real /schedules query, and
            # it used to be issued without consulting the cap at all — so a
            # pilot who had hit their monthly limit still spent on deadhead
            # lookups while ordinary enrichment was paused.
            payer, key = None, None
            for uid in _owners_of(leg_id):
                uid_key = credentials(uid)
                if not uid_key:
                    continue
                try:
                    if budget_state(uid)["exhausted"]:
                        continue
                except Exception:
                    continue
                payer, key = uid, uid_key
                break
            try:
                # With no usable key this still runs, falling back to the
                # free ADS-B probe.
                resolve_carrier(leg, key, now, user_id=payer)
            except Exception as e:
                print(f"[poller] carrier resolution failed for {leg_id}: {e}")
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
            _settle(leg_id, leg, now, has_adsb=False)
            continue
        try:
            record_position(leg_id, state["lat"], state["lon"], now, state.get("on_ground"))
            recorded += 1
        except Exception as e:
            print(f"[poller] record failed for {leg_id}: {e}")

        # We have live data for this leg, so the ADS-B-dependent triggers
        # (wheels down + 5, closeout) can do their job and the no-coverage
        # fallback stays out of the way.
        _settle(leg_id, leg, now, has_adsb=True)
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
