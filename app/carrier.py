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

# How many times we will ever PAY to ask who operates one leg. Two: if
# /schedules doesn't know at T-30 and doesn't know an hour later, it isn't
# going to. The leg then tracks on the free ADS-B probe or not at all,
# which costs the pilot nothing either way.
MAX_PAID_TRIES = 2
PAID_RETRY_GAP = timedelta(minutes=60)

# /schedules costs $0.02, four times the $0.005 that /flights/{ident}
# costs. The local spend counter prices everything at the /flights rate,
# so one schedules lookup has to be counted as four units or the estimate
# under-reports — and the estimate is what enforces the cap whenever
# FlightAware's own usage figure is stale or unreachable.
SCHEDULES_COST_UNITS = 4


def needs_resolution(leg) -> bool:
    """Only deadheads are ambiguous — the pilot's own legs are Envoy."""
    return bool(leg.is_deadhead) and not leg.operator_callsign


def _may_pay(leg, now: datetime) -> bool:
    """Is a paid /schedules lookup allowed for this leg right now?"""
    from .flights import get_flight
    row = get_flight(leg.id)
    if row is None:
        return False
    try:
        tries = int(row["carrier_tries"] or 0)
        last = row["carrier_tried_at"]
    except Exception:
        return False
    if tries >= MAX_PAID_TRIES:
        return False
    if last:
        try:
            when = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if (now - when) < PAID_RETRY_GAP:
                return False
        except Exception:
            pass
    return True


def _record_attempt(leg, now: datetime) -> None:
    """Write the attempt down BEFORE making it.

    Before, not after: if the call raises, times out or the process dies
    mid-request, the attempt still has to count. Recording afterwards is
    how a retry loop survives a crash and starts over.
    """
    from .flights import get_flight, write
    row = get_flight(leg.id)
    tries = 0
    if row is not None:
        try:
            tries = int(row["carrier_tries"] or 0)
        except Exception:
            tries = 0
    write(leg.id, always={"carrier_tries": tries + 1,
                          "carrier_tried_at": now.isoformat()})


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


def resolve(leg, api_key: Optional[str] = None, now: Optional[datetime] = None,
            user_id: Optional[int] = None) -> Optional[str]:
    """Resolve and persist the operating callsign for a leg.

    Returns the callsign if one was found. A success is stored on the row
    and this never runs again for that leg.

    A FAILURE is now recorded too, which it wasn't before v5.2. The poller
    calls this every 20 seconds while a deadhead is in its window, and a
    failed lookup used to write nothing at all — so the next sweep asked
    the identical question, and the sweep after that. On a leg where
    /schedules simply has no answer that was around a thousand billed
    queries in an afternoon, against a budget check this path never
    consulted and a counter it never incremented. One bad deadhead could
    spend the month before anything on screen changed.

    Three things stop that now:

      1. The FREE option goes first. The ADS-B probe costs nothing and
         works whenever the aircraft is sitting at the origin, which is
         most of the time we care about.
      2. Paid attempts are capped at MAX_PAID_TRIES per leg, ever, and
         spaced by PAID_RETRY_GAP. Recorded on the row, so a restart
         doesn't reset the count.
      3. The paid attempt goes through the same budget gate and the same
         counter as every other query.
    """
    if not needs_resolution(leg):
        return leg.operator_callsign

    now = now or datetime.now(timezone.utc)

    # 1. Free first.
    ident = resolve_via_adsb(leg, now)

    # 2. Then pay, if we're allowed to.
    if not ident and api_key and _may_pay(leg, now):
        _record_attempt(leg, now)
        ident = resolve_via_aeroapi(leg, api_key)
        if user_id is not None:
            from .enrichment import _count_query
            _count_query(user_id, now, SCHEDULES_COST_UNITS)

    if not ident:
        return None

    set_operator_callsign(leg.id, ident)
    leg.operator_callsign = ident
    return ident
