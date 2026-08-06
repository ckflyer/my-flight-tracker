"""FlightAware AeroAPI enrichment — the things ADS-B cannot tell you.

Position, altitude, speed, tail number and type all come free from
Airplanes.live and are NOT fetched here. This module exists only for data
the aircraft doesn't broadcast:

  * OOOI — scheduled / estimated / actual gate-out, wheels-off, wheels-on,
    gate-in. Airline and ACARS sourced, so it works with no ADS-B receiver
    anywhere near the field.
  * Delays, as live-updating estimates rather than "still not here".
  * Diversions, with the amended destination.
  * Cancellations, which nothing in ADS-B can ever tell you.
  * Gate, terminal and baggage claim.

COST DISCIPLINE
---------------
This is the pilot's own API key and own money, so every call is
deliberate. Only the background poller calls this module — never a page
render — and results are cached in the database. A family member hammering
refresh during a delay costs nothing.

Endpoint: GET /flights/{ident}, one call returning everything above.
Auth: x-apikey header. Docs: flightaware.com/aeroapi/portal/documentation
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

BASE_URL = os.environ.get("AEROAPI_BASE", "https://aeroapi.flightaware.com/aeroapi")

# FlightAware bills per RESULT SET of up to 15 records, not per HTTP call.
# A response with 20 flight records is two result sets. Counting calls
# would quietly under-report spend.
RESULT_SET_SIZE = 15
REQUEST_TIMEOUT = 15


def _dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _airport_code(block: Optional[Dict[str, Any]]) -> Optional[str]:
    """Prefer the IATA code — that's what an FFDO schedule uses."""
    if not block:
        return None
    return (block.get("code_iata") or block.get("code") or "").strip().upper() or None


def pick_flight(flights: List[Dict[str, Any]], origin: str, destination: str,
                scheduled_dep: Optional[datetime]) -> Optional[Dict[str, Any]]:
    """Choose the flight record that IS this leg.

    /flights/{ident} returns several records for a flight number — recent
    past, in progress, upcoming — and on a turn it returns both directions
    under one number. Matching origin and destination is what makes this
    positive identification rather than inference: the return leg simply
    has them the other way round.

    Where several records still match (same route, different days), the one
    with the closest scheduled departure wins.
    """
    origin = (origin or "").strip().upper()
    destination = (destination or "").strip().upper()

    candidates = []
    for f in flights or []:
        o = _airport_code(f.get("origin"))
        d = _airport_code(f.get("destination"))
        if o != origin:
            continue
        # A diverted flight's destination is amended, so accept a record
        # whose destination no longer matches when it says it diverted —
        # otherwise the leg would go dark exactly when it diverts.
        if d != destination and not f.get("diverted"):
            continue
        candidates.append(f)

    if not candidates:
        return None
    if scheduled_dep is None:
        return candidates[0]

    def gap(f):
        sched = _dt(f.get("scheduled_out")) or _dt(f.get("scheduled_off"))
        if sched is None:
            return float("inf")
        return abs((sched - scheduled_dep).total_seconds())

    # A diversion produces TWO records for the same scheduled slot: the
    # original origin->destination pairing, which the airline may flag
    # cancelled, and the one that actually flew and diverted. They tie on
    # scheduled_out, so a plain min() picked whichever came back first —
    # frequently the cancelled twin, which is why a diversion showed up as
    # "Cancelled" with no destination.
    #
    # Rank by schedule proximity first, then prefer the record with real
    # evidence of flying, then prefer diverted over cancelled.
    def rank(f):
        flew = bool(f.get("actual_off") or f.get("actual_on") or f.get("actual_in"))
        return (
            gap(f),
            0 if flew else 1,
            0 if f.get("diverted") else 1,
            1 if f.get("cancelled") else 0,
        )

    best = min(candidates, key=rank)
    # More than a day away it isn't this leg, whatever the route says.
    return best if gap(best) < 86400 else None


def normalize(f: Dict[str, Any]) -> Dict[str, Any]:
    """One AeroAPI flight record -> the enrichment dict the app stores."""
    return {
        "fa_flight_id": f.get("fa_flight_id"),
        "registration": f.get("registration"),
        "cancelled": bool(f.get("cancelled")),
        "diverted": bool(f.get("diverted")),
        "blocked": bool(f.get("blocked")),
        "status_text": f.get("status"),
        "origin": _airport_code(f.get("origin")),
        "destination": _airport_code(f.get("destination")),
        # OOOI. "actual" is what happened; "estimated" is the live forecast.
        "scheduled_out": f.get("scheduled_out"), "estimated_out": f.get("estimated_out"),
        "actual_out": f.get("actual_out"),
        "scheduled_off": f.get("scheduled_off"), "estimated_off": f.get("estimated_off"),
        "actual_off": f.get("actual_off"),
        "scheduled_on": f.get("scheduled_on"), "estimated_on": f.get("estimated_on"),
        "actual_on": f.get("actual_on"),
        "scheduled_in": f.get("scheduled_in"), "estimated_in": f.get("estimated_in"),
        "actual_in": f.get("actual_in"),
        # Reported in SECONDS by AeroAPI.
        "departure_delay": f.get("departure_delay"),
        "arrival_delay": f.get("arrival_delay"),
        "gate_origin": f.get("gate_origin"),
        "gate_destination": f.get("gate_destination"),
        "terminal_origin": f.get("terminal_origin"),
        "terminal_destination": f.get("terminal_destination"),
        "baggage_claim": f.get("baggage_claim"),
        "source": "aeroapi",
    }


class AeroApiError(Exception):
    pass


def fetch_leg(api_key: str, ident: str, origin: str, destination: str,
              scheduled_dep: Optional[datetime], want_raw: bool = False):
    """One query. Returns the enrichment dict for this leg, or None.

    Raises AeroApiError on auth/quota problems so the caller can surface
    something actionable in Settings — a silently dead toggle is worse than
    no toggle. Ordinary "no matching flight" is just None.
    """
    ident = (ident or "").strip().upper()
    if not api_key or not ident:
        return None

    url = f"{BASE_URL}/flights/{ident}"
    try:
        r = requests.get(url, headers={"x-apikey": api_key},
                         params={"max_pages": 1}, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        raise AeroApiError(f"could not reach AeroAPI: {e}")

    if r.status_code in (401, 403):
        raise AeroApiError("AeroAPI rejected the key (401/403) — check it in Settings")
    if r.status_code == 429:
        raise AeroApiError("AeroAPI rate limit or quota reached (429)")
    if r.status_code >= 500:
        raise AeroApiError(f"AeroAPI server error ({r.status_code})")
    if r.status_code != 200:
        raise AeroApiError(f"AeroAPI returned {r.status_code}")

    try:
        data = r.json()
    except Exception as e:
        raise AeroApiError(f"AeroAPI response was not JSON: {e}")

    records = data.get("flights") or []
    # How many result sets this response actually cost.
    billed = max(1, -(-len(records) // RESULT_SET_SIZE))
    match = pick_flight(records, origin, destination, scheduled_dep)
    if not match:
        return (None, None, billed) if want_raw else None
    normalized = normalize(match)
    normalized["_billed"] = billed
    # The raw record is kept so fields we don't render today don't have to
    # be bought a second time.
    return (normalized, match, billed) if want_raw else normalized


def resolve_operator(api_key: str, flight_number: str, origin: str,
                     destination: str, on_date) -> Optional[str]:
    """Which carrier operates this flight number on this route?

    An FFDO line gives a bare number and a (D) marker. A deadhead is very
    often on mainline American or another wholly-owned regional, so
    assuming ENY looks up a flight that doesn't exist and the leg never
    tracks. Flight number alone is ambiguous; flight number CROSSED WITH
    THE ROUTE is not — only one carrier flies 4110 DFW-LFT on a given day.

    GET /schedules/{start}/{end} answers exactly that. One query per leg,
    ever: a published schedule doesn't change, so the answer is stored on
    the leg and never looked up again.

    Returns an ICAO callsign like "ENY4110" or "AAL2043", or None.
    """
    if not api_key or not flight_number:
        return None
    start = on_date.isoformat()
    end = (on_date + timedelta(days=1)).isoformat()
    url = f"{BASE_URL}/schedules/{start}/{end}"
    try:
        r = requests.get(
            url,
            headers={"x-apikey": api_key},
            params={"origin": origin, "destination": destination,
                    "flight_number": str(flight_number), "max_pages": 1},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:
        raise AeroApiError(f"could not reach AeroAPI: {e}")
    if r.status_code in (401, 403):
        raise AeroApiError("AeroAPI rejected the key (401/403)")
    if r.status_code != 200:
        raise AeroApiError(f"AeroAPI /schedules returned {r.status_code}")

    try:
        rows = r.json().get("scheduled") or []
    except Exception:
        return None

    want_o = (origin or "").strip().upper()
    want_d = (destination or "").strip().upper()
    for row in rows:
        o = (row.get("origin_iata") or row.get("origin") or "").strip().upper()
        d = (row.get("destination_iata") or row.get("destination") or "").strip().upper()
        if o != want_o or d != want_d:
            continue
        # actual_ident is the OPERATING flight when the queried one is a
        # codeshare — AAL4110 marketed, ENY4110 operated. The operating
        # carrier is what gets broadcast on ADS-B, so that's what we need.
        ident = (row.get("actual_ident_icao") or row.get("actual_ident")
                 or row.get("ident_icao") or row.get("ident"))
        if ident:
            return str(ident).strip().upper()
    return None


def fetch_usage(api_key: str, start: Optional[str] = None,
                end: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """FlightAware's OWN figure for what this key has spent.

    GET /account/usage costs nothing, so this replaces our estimate with
    the authoritative number. Two caveats from FlightAware's docs: the
    figure updates every 10 minutes rather than in real time, and it
    doesn't account for monthly minimums (irrelevant on the Personal tier,
    which has none).

    The response shape isn't something this code can verify from here, so
    it reads defensively and hands back the raw payload alongside whatever
    it managed to parse — a caller that gets `cost: None` should fall back
    to the local estimate rather than assume zero spend.
    """
    if not api_key:
        return None
    params = {}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    try:
        r = requests.get(f"{BASE_URL}/account/usage",
                         headers={"x-apikey": api_key},
                         params=params or None, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print(f"[aeroapi] usage lookup failed: {e}")
        return None
    if r.status_code != 200:
        print(f"[aeroapi] usage lookup returned {r.status_code}")
        return None
    try:
        data = r.json()
    except Exception:
        return None

    def _dig(obj, keys):
        """Pull the first matching key, at the top level or one level down."""
        for k in keys:
            if isinstance(obj, dict) and obj.get(k) is not None:
                return obj[k]
        if isinstance(obj, dict):
            for v in obj.values():
                if isinstance(v, dict):
                    found = _dig(v, keys)
                    if found is not None:
                        return found
                elif isinstance(v, list) and v and isinstance(v[0], dict):
                    total = 0.0
                    hit = False
                    for row in v:
                        got = _dig(row, keys)
                        if isinstance(got, (int, float)):
                            total += got
                            hit = True
                    if hit:
                        return total
        return None

    cost = _dig(data, ["total_cost", "cost", "total_charges", "charges"])
    calls = _dig(data, ["total_calls", "calls", "total_queries", "queries", "count"])
    return {
        "cost": float(cost) if isinstance(cost, (int, float)) else None,
        "calls": int(calls) if isinstance(calls, (int, float)) else None,
        "raw": data,
    }
