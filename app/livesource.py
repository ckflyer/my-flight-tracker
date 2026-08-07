"""Live aircraft data — provider-agnostic front door.

Everything upstream of this module (main.py, track.py, the templates) reads
the NORMALIZED STATE DICT and never touches a provider directly:

    callsign        str   | None   callsign as broadcast
    icao24          str   | None   24-bit ICAO hex, lowercase
    lat, lon        float | None   decimal degrees
    on_ground       bool            True when the aircraft is on the surface
    altitude_ft     int   | None   None while on the ground
    speed_kts       int   | None   ground speed
    track           float | None   degrees true, 0-359
    registration    str   | None   tail number, e.g. "N204NN"
    type_code       str   | None   ICAO type code, e.g. "E75L"
    aircraft_type   str   | None   human-readable, e.g. "Embraer 175"
    position_age_s  float | None   seconds since the fix was observed
    source          str             which provider produced this

To switch providers, write a module exposing `fetch_state(callsign)` that
returns this shape and change the import below. That's the whole job —
this indirection exists specifically so going back to OpenSky (or on to
something else) is a one-line change rather than an excavation.

Caching and rate limiting live HERE rather than in the provider so they
apply to whichever provider is plugged in.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

from .airplaneslive import fetch_state as _provider_fetch_state

# airplanes.live allows 1 request/second for the whole deployment — not per
# user. Every viewer polls independently, so without a floor here, the
# pilot plus three family members watching the same flight would each fire
# their own upstream request and collectively blow the limit.
MIN_UPSTREAM_INTERVAL_S = 1.2

# How long a fetched state stays fresh enough to hand to another caller.
# Set below the client poll interval so a viewer polling every 10s gets
# genuinely new data each time, while simultaneous viewers share one fetch.
DEFAULT_CACHE_TTL_S = 8.0

_lock = threading.Lock()
_cache: Dict[str, Dict[str, Any]] = {}   # callsign -> {"at": float, "state": dict|None}
_last_upstream_at: float = 0.0


def _cached(cs: str, ttl: float, now: float) -> Optional[Dict[str, Any]]:
    entry = _cache.get(cs)
    if entry and (now - entry["at"]) < ttl:
        return entry
    return None


def live_state(callsign: str, cache_ttl_s: float = DEFAULT_CACHE_TTL_S) -> Optional[Dict[str, Any]]:
    """Current normalized state for a callsign, or None if not tracked.

    Concurrent callers asking for the same callsign collapse into a single
    upstream request: the first one through fetches while holding the lock,
    the rest find a fresh cache entry when they acquire it.

    A None result is cached like any other. "This flight isn't showing up
    on ADS-B right now" is a real answer, and re-asking every 300ms while
    four people watch a plane that's still at the gate would burn the rate
    limit for nothing.
    """
    global _last_upstream_at
    cs = (callsign or "").strip().upper()
    if not cs:
        return None

    now = time.monotonic()
    entry = _cached(cs, cache_ttl_s, now)
    if entry is not None:
        return entry["state"]

    with _lock:
        # Re-check inside the lock: while we waited, another thread may
        # have fetched exactly what we're about to ask for.
        now = time.monotonic()
        entry = _cached(cs, cache_ttl_s, now)
        if entry is not None:
            return entry["state"]

        # Global floor between upstream calls, across all callsigns and all
        # viewers. Serving a slightly stale entry beats getting throttled.
        since_last = now - _last_upstream_at
        if since_last < MIN_UPSTREAM_INTERVAL_S:
            stale = _cache.get(cs)
            if stale is not None:
                return stale["state"]
            time.sleep(MIN_UPSTREAM_INTERVAL_S - since_last)

        state = _provider_fetch_state(cs)
        _last_upstream_at = time.monotonic()
        _cache[cs] = {"at": _last_upstream_at, "state": state}

        # Keep the cache from growing without bound across a long-running
        # process. Only ever a handful of callsigns in practice.
        if len(_cache) > 64:
            oldest = sorted(_cache.items(), key=lambda kv: kv[1]["at"])[:32]
            for k, _ in oldest:
                _cache.pop(k, None)

        return state


def live_summary(callsign: str, cache_ttl_s: float = DEFAULT_CACHE_TTL_S) -> Optional[Dict[str, Any]]:
    """Backwards-compatible alias. The old OpenSky client exposed
    live_summary(); keeping the name means main.py's call sites read the
    same as before."""
    return live_state(callsign, cache_ttl_s)


def live_state_for_leg(leg, now, history=None):
    """Live state for a leg, or None if this isn't the leg's aircraft.

    Every consumer — the page, the poll endpoint, the background recorder —
    goes through here, so the guard can't be applied in one path and
    forgotten in another.
    """
    from .flightmatch import evaluate, observe, release_aircraft

    state = live_state(leg.callsign)
    if state is None:
        return None

    verdict = evaluate(leg, state, now)
    if not verdict.accepted:
        print(f"[livesource] ignoring aircraft on {leg.callsign} for {leg.id}: {verdict.reason}")
        return None

    # Fold this observation into the leg's flight-cycle state. If it
    # completes the leg (flown, then stopped on the ground — or a new
    # squawk while parked), this is the last observation this leg accepts.
    if observe(leg, state, now):
        print(f"[livesource] {leg.id} complete — aircraft has flown and is stopped "
              f"on the ground; releasing the callsign")
    return state


def reset_cache() -> None:
    """Test hook — clears cache and the rate-limit clock."""
    global _last_upstream_at
    with _lock:
        _cache.clear()
        _last_upstream_at = 0.0
