"""
OpenSky Network client – free ADS-B live data.
Uses OAuth2 client credentials. Credentials via environment variables:
  OPENSKY_CLIENT_ID
  OPENSKY_CLIENT_SECRET
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import requests

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
API_BASE = "https://opensky-network.org/api"

# Cache token and last successful state to avoid burning credits
_token: Optional[str] = None
_token_expires: float = 0
_last_state_cache: Dict[str, Any] = {}
_last_fetch_time: float = 0
def _min_interval() -> int:
    try:
        from .settings import load_settings
        return max(20, int(load_settings().poll_seconds))
    except Exception:
        return 45


def _get_credentials() -> tuple[str, str]:
    cid = os.environ.get("OPENSKY_CLIENT_ID", "").strip()
    secret = os.environ.get("OPENSKY_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        try:
            from .settings import load_settings
            s = load_settings()
            cid = cid or (s.opensky_client_id or "").strip()
            secret = secret or (s.opensky_client_secret or "").strip()
        except Exception:
            pass
    return cid, secret


def _get_token() -> Optional[str]:
    global _token, _token_expires
    now = time.time()
    if _token and now < _token_expires - 30:
        return _token

    cid, secret = _get_credentials()
    if not cid or not secret:
        return None

    try:
        r = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": cid,
                "client_secret": secret,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        _token = data["access_token"]
        _token_expires = now + int(data.get("expires_in", 1800))
        return _token
    except Exception as e:
        print(f"[opensky] token error: {e}")
        return None


def _normalize_callsign(cs: str) -> str:
    """OpenSky callsigns are often space-padded to 8 chars."""
    return cs.strip().upper()


def fetch_state_by_callsign(callsign: str) -> Optional[Dict[str, Any]]:
    """
    Search live states for a matching callsign (e.g. ENY3916).
    Returns a simplified dict or None.
    Rate-limited and cached briefly.
    """
    global _last_state_cache, _last_fetch_time

    cs = _normalize_callsign(callsign)
    now = time.time()

    # Return recent cache if we polled very recently
    if cs in _last_state_cache and (now - _last_fetch_time) < _min_interval():
        return _last_state_cache.get(cs)

    token = _get_token()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        # Global states is expensive; still ok for personal use with caching.
        # Authenticated users get better limits.
        r = requests.get(
            f"{API_BASE}/states/all",
            headers=headers,
            timeout=20,
        )
        if r.status_code == 429:
            print("[opensky] rate limited")
            return _last_state_cache.get(cs)
        r.raise_for_status()
        data = r.json()
        states = data.get("states") or []

        match = None
        for s in states:
            # indices: 0=icao24, 1=callsign, 5=lon, 6=lat, 7=baro_alt, 8=on_ground,
            # 9=velocity, 10=true_track, 11=vertical_rate, 13=geo_alt
            raw_cs = (s[1] or "").strip().upper()
            if not raw_cs:
                continue
            # Match ENY3916, ENY3916 , AAL3916, etc. – prefer exact ENY
            if raw_cs == cs or raw_cs.startswith(cs) or cs in raw_cs:
                match = s
                if raw_cs == cs or raw_cs.startswith("ENY"):
                    break  # prefer exact / ENY

        _last_fetch_time = now

        if not match:
            _last_state_cache[cs] = None
            return None

        result = {
            "icao24": match[0],
            "callsign": (match[1] or "").strip(),
            "longitude": match[5],
            "latitude": match[6],
            "baro_altitude_m": match[7],
            "baro_altitude_ft": int(match[7] * 3.28084) if match[7] is not None else None,
            "on_ground": bool(match[8]),
            "velocity_ms": match[9],
            "velocity_kts": int(match[9] * 1.94384) if match[9] is not None else None,
            "track": match[10],
            "vertical_rate_ms": match[11],
            "geo_altitude_m": match[13],
            "geo_altitude_ft": int(match[13] * 3.28084) if match[13] is not None else None,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        }
        _last_state_cache[cs] = result
        return result

    except Exception as e:
        print(f"[opensky] fetch error: {e}")
        return _last_state_cache.get(cs)


def live_summary(callsign: str) -> Optional[Dict[str, Any]]:
    """Human-friendly summary for the UI."""
    st = fetch_state_by_callsign(callsign)
    if not st:
        return None

    alt = st.get("baro_altitude_ft") or st.get("geo_altitude_ft")
    spd = st.get("velocity_kts")
    parts = {
        "callsign": st.get("callsign"),
        "icao24": st.get("icao24"),
        "on_ground": st.get("on_ground"),
        "altitude_ft": alt,
        "speed_kts": spd,
        "track": st.get("track"),
        "lat": st.get("latitude"),
        "lon": st.get("longitude"),
        "map_url": None,
    }
    if st.get("latitude") is not None and st.get("longitude") is not None:
        # Simple OSM / Google-style deep link
        parts["map_url"] = (
            f"https://www.openstreetmap.org/?mlat={st['latitude']}&mlon={st['longitude']}"
            f"#map=8/{st['latitude']}/{st['longitude']}"
        )
    return parts
