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

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from .db import get_connection

# WHY THERE IS A LIST HERE RATHER THAN ONE URL.
#
# airplanes.live closed its public API. The endpoint still answers, but with
# HTTP 403 and a message asking you to email them for access — so every
# lookup silently returned None and no aircraft was ever tracked, while the
# schedule and AeroAPI carried on working normally. Nothing in this app was
# broken; the door was shut from the other side.
#
# Several other feeds expose the SAME response shape, because they all run
# the same underlying tar1090/re-api software. That means one parser serves
# all of them and switching is a matter of which host answers, not new code.
# Enabled feeds are tried in order and the first that works is remembered.
#
# The list lives in the database so it can be edited from the diagnostics
# page without a redeploy — the whole point being that when a feed shuts
# down, the fix is choosing a different line, not shipping code.
DEFAULT_ENDPOINTS = [
    {"url": "https://api.adsb.lol/v2", "enabled": True, "api_key": ""},
    {"url": "https://opendata.adsb.fi/api/v2", "enabled": True, "api_key": ""},
    # Off by default since 1.8.0. airplanes.live withdrew its free API to
    # stay solvent — their words: 2 billion requests a week, monthly egress
    # burned through in four days, hosting up ~300% in 18 months. It is now
    # $25/mo sponsorship, OR FREE FROM A FEEDER'S OWN IP. If you run a
    # receiver or sponsor them, enable this and put it first: it is the
    # best-coverage unfiltered feed of the three.
    {"url": "https://api.airplanes.live/v2", "enabled": False, "api_key": ""},
]

# Attribution required by adsb.fi's terms, and only fair to the others.
# Surfaced on the diagnostics panel rather than buried in a licence file.
FEED_CREDITS = [
    ("adsb.lol", "https://adsb.lol", "ODbL 1.0, open to everyone"),
    ("adsb.fi", "https://adsb.fi", "personal, non-commercial use only"),
    ("airplanes.live", "https://airplanes.live", "feeders and sponsors only"),
]

_META_KEY = "adsb_endpoints"


def _env_pin():
    """Environment always wins, so a deployment can be forced from compose."""
    pinned = os.environ.get("AIRPLANES_LIVE_BASE", "").strip()
    if pinned:
        return [{"url": pinned.rstrip("/"), "enabled": True,
                 "api_key": os.environ.get("ADSB_API_KEY", "").strip(),
                 "locked": True}]
    raw = os.environ.get("ADSB_ENDPOINTS", "").strip()
    if raw:
        key = os.environ.get("ADSB_API_KEY", "").strip()
        return [{"url": u.strip().rstrip("/"), "enabled": True,
                 "api_key": key, "locked": True}
                for u in raw.split(",") if u.strip()]
    return None


def load_endpoints():
    """Configured feeds, newest state from the database.

    Read on every sweep rather than cached at import, so an edit on the
    diagnostics page takes effect on the next poll instead of the next
    restart.
    """
    pinned = _env_pin()
    if pinned:
        return pinned
    try:
        conn = get_connection()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS app_meta ("
                         "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            r = conn.execute("SELECT value FROM app_meta WHERE key = ?",
                             (_META_KEY,)).fetchone()
        finally:
            conn.close()
        if r and r["value"]:
            data = json.loads(r["value"])
            out = []
            for e in data:
                if isinstance(e, dict) and e.get("url"):
                    out.append({"url": str(e["url"]).rstrip("/"),
                                "enabled": bool(e.get("enabled", True)),
                                "api_key": str(e.get("api_key") or "")})
            if out:
                return out
    except Exception as e:
        print(f"[adsb] could not read endpoint list: {e}")
    return [dict(e) for e in DEFAULT_ENDPOINTS]


def reset_endpoints() -> None:
    """Forget any saved feed list and fall back to DEFAULT_ENDPOINTS.

    Needed because `load_endpoints` prefers whatever is stored in the
    database, so an install that saved a list back when airplanes.live was
    the default stays pinned to a feed that now returns 403 — while a fresh
    install of the same version works perfectly. That is a difference no
    amount of reading the code explains, so there is a button for it.
    """
    # app_meta, not meta — the table this module already READS from three
    # lines up. Getting it wrong meant the button crashed with a 500 on
    # exactly the installs that most needed it, since a table only exists
    # once something has been written to it.
    conn = get_connection()
    try:
        conn.execute("DELETE FROM app_meta WHERE key = ?", (_META_KEY,))
        conn.commit()
    except Exception as e:
        # Nothing was ever saved, so there is nothing to forget and the
        # defaults are already in force. That is a success, not an error.
        print(f"[adsb] nothing to reset: {e}")
    finally:
        conn.close()


def save_endpoints(endpoints) -> None:
    clean = []
    for e in endpoints:
        url = (e.get("url") or "").strip().rstrip("/")
        if not url:
            continue
        clean.append({"url": url,
                      "enabled": bool(e.get("enabled", True)),
                      "api_key": (e.get("api_key") or "").strip()})
    conn = get_connection()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS app_meta ("
                     "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
                     (_META_KEY, json.dumps(clean)))
        conn.commit()
    finally:
        conn.close()
    global _preferred
    _preferred = None      # re-discover against the new list


def endpoints_are_locked() -> bool:
    """True when compose is dictating the list, so the UI can say so
    instead of silently ignoring edits."""
    return _env_pin() is not None



# ---------------------------------------------------------------------------
# What actually happened, kept in the database.
#
# The diagnostics page could only show a synthetic probe of callsign AAL100,
# fired at the moment the page was opened. That answers "is this host up
# right now", which is NOT the same question as "did the poller get a
# position for my flight" — the two can and do disagree. A feed that
# rate-limits a burst of probes looks dead on the page while serving the
# poller perfectly a minute later.
#
# So every real lookup records its outcome here, and the page shows that
# alongside the probe. Survives restarts, because it is the history that
# makes an intermittent fault legible.
# ---------------------------------------------------------------------------

_LOG_KEY = "adsb_last_results"
_LOG_MAX = 40


def record_result(feed: str, callsign: str, ok: bool, detail: str) -> None:
    try:
        conn = get_connection()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS app_meta ("
                         "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            r = conn.execute("SELECT value FROM app_meta WHERE key = ?",
                             (_LOG_KEY,)).fetchone()
            log = json.loads(r["value"]) if r and r["value"] else []
        except Exception:
            log = []
        log.insert(0, {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "feed": feed, "callsign": callsign, "ok": bool(ok),
            "detail": detail[:160],
        })
        del log[_LOG_MAX:]
        conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
                     (_LOG_KEY, json.dumps(log)))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[adsb] could not record result: {e}")


def recent_results():
    try:
        conn = get_connection()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS app_meta ("
                         "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            r = conn.execute("SELECT value FROM app_meta WHERE key = ?",
                             (_LOG_KEY,)).fetchone()
        finally:
            conn.close()
        return json.loads(r["value"]) if r and r["value"] else []
    except Exception:
        return []


# Which endpoint last answered. Tried first next time so a working feed is
# not re-discovered on every single sweep.
_preferred: Optional[str] = None

REQUEST_TIMEOUT = 12

# airplanes.live returns `t` as an ICAO type code ("E75L"). Nothing in the
# response spells the aircraft out in words, so this maps the types an
# regional crew member actually flies into something a family member
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
        "squawk": (ac.get("squawk") or "").strip() or None,
        "position_age_s": position_age,
        "source": _preferred or "adsb",
    }


def probe(base: str, callsign: str = "AAL100", api_key: str = ""):
    """One raw request, for the diagnostics page.

    Returns (status_code|None, aircraft_count|None, short_message).

    Goes through the SAME global throttle the poller uses. It did not
    before 1.8.0, so opening the admin page fired one request per feed with
    no spacing between them, and every feed after the first came back 429.
    The page then reported all feeds dead while tracking was working fine.
    """
    from .livesource import throttle
    try:
        throttle()
        r = _get(base, callsign, api_key)
    except Exception as e:
        return None, None, f"unreachable: {e}"
    if r.status_code == 429:
        # NOT a failure, and must not be reported as one. It means the feed
        # answered and told us to slow down, which is proof it is alive.
        return 429, None, "rate limited (feed is up; probe asked too fast)"
    if r.status_code != 200:
        body = (r.text or "").strip().replace("\n", " ")
        return r.status_code, None, body[:180] or "(empty response)"
    try:
        data = r.json()
    except Exception as e:
        return r.status_code, None, f"not JSON: {e}"
    if not isinstance(data, dict):
        return r.status_code, None, "unexpected shape (not an object)"
    ac = data.get("ac") or data.get("aircraft")
    if ac is None:
        return r.status_code, None, "no 'ac' list — different API format"
    return r.status_code, len(ac), "ok"


def _get(base: str, cs: str, api_key: str = ""):
    headers = {"Accept": "application/json"}
    if api_key:
        # Sent both ways: providers in this family variously want a bearer
        # token or their own header, and an unrecognised header is ignored.
        headers["Authorization"] = f"Bearer {api_key}"
        headers["api-auth"] = api_key
    return requests.get(f"{base}/callsign/{cs}", timeout=REQUEST_TIMEOUT,
                        headers=headers)


def _parse(data) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    ac = _first_with_position(data.get("ac") or data.get("aircraft") or [])
    if not ac:
        return None
    return normalize(ac)


def fetch_state(callsign: str) -> Optional[Dict[str, Any]]:
    """Live state for one callsign, or None if it isn't currently tracked.

    Tries each configured feed until one answers usefully. A feed that
    returns 403/404/error is skipped rather than treated as "no aircraft",
    which is the distinction that made the airplanes.live shutdown look
    like every flight simply having no ADS-B coverage.

    Still returns None on total failure — callers treat "no data" and
    "lookup failed" identically, since both mean falling back to the
    schedule.
    """
    global _preferred
    cs = (callsign or "").strip().upper()
    if not cs:
        return None

    order = [e for e in load_endpoints() if e.get("enabled", True)]
    if _preferred:
        order.sort(key=lambda e: e["url"] != _preferred)

    last_error = None
    for entry in order:
        base = entry["url"]
        try:
            r = _get(base, cs, entry.get("api_key") or "")
        except Exception as e:
            last_error = f"{base}: {e}"
            continue
        if r.status_code == 429:
            last_error = f"{base}: rate limited"
            continue
        if r.status_code != 200:
            last_error = f"{base}: HTTP {r.status_code}"
            continue
        try:
            data = r.json()
        except Exception as e:
            last_error = f"{base}: bad JSON ({e})"
            continue
        if not isinstance(data, dict) or (
                data.get("ac") is None and data.get("aircraft") is None):
            last_error = f"{base}: unrecognised response format"
            continue

        # This feed is healthy. Remember it even when this particular
        # callsign has nothing flying — an empty list is a real answer.
        if _preferred != base:
            print(f"[adsb] using {base}")
            _preferred = base
        try:
            state = _parse(data)
        except Exception as e:
            print(f"[adsb] parse error from {base}: {e}")
            record_result(base, cs, False, f"parse error: {e}")
            return None
        n = len(data.get("ac") or data.get("aircraft") or [])
        record_result(base, cs, True,
                      "position found" if state else
                      f"feed healthy, {n} aircraft on this callsign, none with a position")
        return state

    if last_error:
        print(f"[adsb] no usable feed — last error: {last_error}")
        record_result("(none)", cs, False, last_error)
    return None
