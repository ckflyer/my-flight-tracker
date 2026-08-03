"""Airplanes.live ADS-B provider.

Docs: https://airplanes.live/api-guide/
Fields: https://airplanes.live/rest-api-adsb-data-field-descriptions/

No API key, no OAuth, no account. Queries one callsign directly
(GET /v2/callsign/ENY3729) rather than pulling every aircraft on earth and
filtering locally, which is what the old OpenSky client had to do.

This module's ONLY job is to turn one provider's JSON into the normalized
state dict defined in livesource.py. It does no caching, no rate limiting,
and knows nothing about flights, legs, or users — so swapping providers
means writing one more module with a `fetch_state()` of the same shape and
changing a single import in livesource.py. Nothing downstream can tell
which service the data came from.

Rate limiting and caching live in livesource.py, deliberately, so they
apply no matter which provider is plugged in.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

import requests

# Overridable so tests can point at a local fixture server. Not a user
# setting — there's no reason for the pilot to change this.
BASE_URL = os.environ.get("AIRPLANES_LIVE_BASE", "https://api.airplanes.live/v2")

REQUEST_TIMEOUT = 12

# airplanes.live returns `t` as an ICAO type code ("E75L"). Nothing in the
# response spells the aircraft out in words, so this maps the types an
# Envoy/American Eagle pilot actually flies into something a family member
# reading the page would recognize. Anything not listed falls back to the
# raw code, which is still better than showing nothing.
TYPE_NAMES = {
    "E75L": "Embraer 175",
    "E75S": "Embraer 175",
    "E170": "Embraer 170",
    "E145": "Embraer ERJ-145",
    "E135": "Embraer ERJ-135",
    "CRJ2": "Bombardier CRJ200",
    "CRJ7": "Bombardier CRJ700",
    "CRJ9": "Bombardier CRJ900",
    "B738": "Boeing 737-800",
    "B38M": "Boeing 737 MAX 8",
    "A319": "Airbus A319",
    "A320": "Airbus A320",
    "A321": "Airbus A321",
}


def describe_type(type_code: Optional[str]) -> Optional[str]:
    if not type_code:
        return None
    code = type_code.strip().upper()
    if not code:
        return None
    return TYPE_NAMES.get(code, code)


def _first_with_position(aircraft: list) -> Optional[Dict[str, Any]]:
    """Pick the best entry from a callsign match.

    The endpoint can return more than one aircraft for a callsign — most
    often a stale duplicate, occasionally a same-callsign flight elsewhere.
    Prefer an entry that actually has a usable position over one that
    doesn't, rather than blindly taking index 0.
    """
    if not aircraft:
        return None
    for ac in aircraft:
        if ac.get("lat") is not None and ac.get("lon") is not None:
            return ac
    for ac in aircraft:
        last = ac.get("lastPosition") or {}
        if last.get("lat") is not None and last.get("lon") is not None:
            return ac
    return aircraft[0]


def normalize(ac: Dict[str, Any]) -> Dict[str, Any]:
    """One aircraft object -> the normalized state dict (see livesource)."""
    lat, lon = ac.get("lat"), ac.get("lon")
    position_age = ac.get("seen_pos")

    # Regular lat/lon go stale after 60s, at which point the API moves the
    # last known fix into `lastPosition` instead. Falling back to it keeps
    # the plane on the map through a coverage gap rather than blinking out.
    if lat is None or lon is None:
        last = ac.get("lastPosition") or {}
        if last.get("lat") is not None and last.get("lon") is not None:
            lat, lon = last.get("lat"), last.get("lon")
            position_age = last.get("seen_pos", position_age)

    # THE IMPORTANT ONE. There is no `on_ground` boolean in this API.
    # Ground state is encoded by `alt_baro` being the STRING "ground"
    # instead of a number. The whole phase state machine in track.py
    # (Taxi-out / Taxi-in / Arrived) keys off on_ground, so getting this
    # wrong means a flight reads as airborne forever and never reports
    # having landed.
    alt_baro = ac.get("alt_baro")
    on_ground = isinstance(alt_baro, str) and alt_baro.strip().lower() == "ground"

    altitude_ft = None
    if not on_ground:
        if isinstance(alt_baro, (int, float)):
            altitude_ft = int(alt_baro)
        elif isinstance(ac.get("alt_geom"), (int, float)):
            altitude_ft = int(ac["alt_geom"])

    speed_kts = int(ac["gs"]) if isinstance(ac.get("gs"), (int, float)) else None
    track = ac.get("track")
    if not isinstance(track, (int, float)):
        # Sitting still on a ramp often means no valid ground track; the
        # transponder heading is the next best thing for pointing the icon.
        heading = ac.get("true_heading")
        if not isinstance(heading, (int, float)):
            heading = ac.get("mag_heading")
        track = heading if isinstance(heading, (int, float)) else None

    registration = (ac.get("r") or "").strip() or None
    type_code = (ac.get("t") or "").strip() or None

    return {
        "callsign": (ac.get("flight") or "").strip() or None,
        "icao24": (ac.get("hex") or "").strip().lower() or None,
        "lat": lat,
        "lon": lon,
        "on_ground": on_ground,
        "altitude_ft": altitude_ft,
        "speed_kts": speed_kts,
        "track": track,
        "registration": registration,
        "type_code": type_code,
        "aircraft_type": describe_type(type_code),
        "position_age_s": position_age,
        "source": "airplanes.live",
    }


def fetch_state(callsign: str) -> Optional[Dict[str, Any]]:
    """Live state for one callsign, or None if it isn't currently tracked.

    Returns None on any failure (offline, timeout, rate limited, malformed
    response). Callers treat "no data" and "lookup failed" identically —
    both mean the page falls back to schedule-based status — so there's
    nothing useful to distinguish here.
    """
    cs = (callsign or "").strip().upper()
    if not cs:
        return None
    try:
        r = requests.get(f"{BASE_URL}/callsign/{cs}", timeout=REQUEST_TIMEOUT)
        if r.status_code == 429:
            print("[airplaneslive] rate limited")
            return None
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[airplaneslive] fetch error: {e}")
        return None

    if not isinstance(data, dict):
        return None
    ac = _first_with_position(data.get("ac") or data.get("aircraft") or [])
    if not ac:
        return None
    try:
        return normalize(ac)
    except Exception as e:
        print(f"[airplaneslive] parse error: {e}")
        return None
