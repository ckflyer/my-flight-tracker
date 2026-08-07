"""Who actually operates this flight?

An FFDO line gives a bare flight number and, for a deadhead, a "(D)".
It never says which airline. For the pilot's own legs that's fine — they're
Envoy. For a deadhead it usually isn't: those are typically mainline
American or another wholly-owned regional, each of which broadcasts its
own callsign on ADS-B. Looking up ENY4110 when the aircraft is squawking
AAL4110 means the leg simply never tracks.

Flight number alone can't answer it. Flight number CROSSED WITH THE ROUTE
can: only one carrier operates 4110 DFW-LFT on a given day.

Two ways to get there, both resolving once per leg and then stored:

  1. AeroAPI /schedules — definitive, one query, needs a key.
  2. ADS-B probing — free. Try the handful of callsigns American's family
     actually uses and see which one has an aircraft at the origin around
     departure. Worse, but it means deadheads aren't dark without a key.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .geo import haversine_nm
from .schedule import set_operator_callsign

# American's mainline plus the wholly-owned regionals a deadhead realistically
# lands on. Envoy first, since that's the common case and usually ends the
# search on the first try.
CANDIDATE_PREFIXES: List[str] = [
    "ENY",   # Envoy Air
    "AAL",   # American mainline
    "JIA",   # PSA Airlines
    "PDT",   # Piedmont Airlines
]

# How close to the origin an aircraft must be for a candidate callsign to
# count as "this is the flight". Same radius the acquisition logic uses.
PROBE_RADIUS_NM = 40.0


def needs_resolution(leg) -> bool:
    """Only deadheads are ambiguous — the pilot's own legs are Envoy."""
    return bool(leg.is_deadhead) and not leg.operator_callsign


def resolve_via_aeroapi(leg, api_key: str) -> Optional[str]:
    from .aeroapi import AeroApiError, resolve_operator
    try:
        ident = resolve_operator(api_key, leg.flight_number, leg.origin,
                                 leg.destination, leg.date)
    except AeroApiError as e:
        print(f"[carrier] {leg.id}: schedules lookup failed: {e}")
        return None
    if ident:
        print(f"[carrier] {leg.id}: operated by {ident} (AeroAPI schedules)")
    return ident


def resolve_via_adsb(leg, now: Optional[datetime] = None) -> Optional[str]:
    """Free fallback: probe candidate callsigns for an aircraft at the origin.

    Only worth doing around departure — that's the one moment the right
    aircraft is reliably identifiable by position. A handful of lookups
    once per leg, not per poll, and the shared cache absorbs them.
    """
    from .livesource import live_state

    oi = leg.origin_info
    if not oi or oi.lat is None:
        return None
    now = now or datetime.now(timezone.utc)
    dep = leg.dep_datetime_utc()
    if dep and not (dep - timedelta(minutes=45) <= now <= dep + timedelta(minutes=45)):
        return None   # outside the window where "at the origin" means anything

    for prefix in CANDIDATE_PREFIXES:
        callsign = f"{prefix}{leg.flight_number}"
        try:
            state = live_state(callsign)
        except Exception:
            continue
        if not state or state.get("lat") is None:
            continue
        if haversine_nm(state["lat"], state["lon"], oi.lat, oi.lon) <= PROBE_RADIUS_NM:
            print(f"[carrier] {leg.id}: {callsign} found at {leg.origin} — "
                  f"treating as the operator")
            return callsign
    return None


def resolve(leg, api_key: Optional[str] = None,
            now: Optional[datetime] = None) -> Optional[str]:
    """Resolve and persist the operating callsign for a leg.

    Returns the callsign if one was found. Stored on the leg, so this runs
    once rather than on every poll.
    """
    if not needs_resolution(leg):
        return leg.operator_callsign

    ident = resolve_via_aeroapi(leg, api_key) if api_key else None
    if not ident:
        ident = resolve_via_adsb(leg, now)
    if not ident:
        return None

    set_operator_callsign(leg.id, ident)
    leg.operator_callsign = ident
    return ident
