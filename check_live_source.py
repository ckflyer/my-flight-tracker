#!/usr/bin/env python3
"""Check the live ADS-B source against a real callsign.

Run this ON THE SERVER (it needs outbound internet):

    cd /path/to/flight-tracker
    python3 check_live_source.py ENY3729

Why this exists: the adapter was written and tested against the published
airplanes.live v2 schema using fixtures, because the machine it was built
on can't reach api.airplanes.live. This script closes that gap — it hits
the real API, prints exactly what came back, and flags anything the
adapter would mishandle.

Pick a callsign that is AIRBORNE RIGHT NOW, otherwise you'll just get
"not currently tracked", which tells you the network works but nothing
about the response shape. Any airline flight in the air will do — it does
not have to be one of yours.
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import requests
except ImportError:
    print("requests not installed — run this inside the app's venv/container")
    sys.exit(2)

from app.airplaneslive import BASE_URL, normalize

callsign = (sys.argv[1] if len(sys.argv) > 1 else "").strip().upper()
if not callsign:
    print(__doc__)
    sys.exit(2)

url = f"{BASE_URL}/callsign/{callsign}"
print(f"GET {url}\n")

try:
    r = requests.get(url, timeout=15)
except Exception as e:
    print(f"NETWORK FAILURE: {e}")
    print("\nThe server couldn't reach the API. Check outbound DNS/HTTPS.")
    sys.exit(1)

print(f"HTTP {r.status_code}")
if r.status_code == 429:
    print("Rate limited (1 req/sec). Wait a moment and retry.")
    sys.exit(1)
if r.status_code != 200:
    print(r.text[:500])
    sys.exit(1)

try:
    data = r.json()
except Exception as e:
    print(f"Response was not JSON: {e}\n{r.text[:500]}")
    sys.exit(1)

envelope_key = "ac" if "ac" in data else ("aircraft" if "aircraft" in data else None)
print(f"envelope key: {envelope_key!r}   top-level keys: {sorted(data.keys())}")
if envelope_key is None:
    print("\nPROBLEM: response has neither 'ac' nor 'aircraft'. The API shape "
          "changed; app/airplaneslive.py needs updating.")
    sys.exit(1)

aircraft = data.get(envelope_key) or []
print(f"aircraft returned: {len(aircraft)}\n")
if not aircraft:
    print(f"{callsign} isn't currently being tracked. Network and response "
          f"shape are fine — retry with a callsign that's airborne now.")
    sys.exit(0)

ac = aircraft[0]
print("--- RAW (first aircraft) ---")
print(json.dumps(ac, indent=2, sort_keys=True)[:2000])

print("\n--- NORMALIZED (what the app will use) ---")
state = normalize(ac)
for k, v in state.items():
    print(f"  {k:16} {v!r}")

print("\n--- CHECKS ---")
problems = []


def report(ok, msg, fatal=True):
    print(f"  {'ok  ' if ok else 'WARN'} {msg}")
    if not ok and fatal:
        problems.append(msg)


report(state["lat"] is not None and state["lon"] is not None,
       "position present")
report("alt_baro" in ac, "alt_baro field present")

alt = ac.get("alt_baro")
if isinstance(alt, str):
    report(alt.strip().lower() == "ground",
           f"alt_baro is the string {alt!r} -> on_ground={state['on_ground']}")
    report(state["on_ground"] is True,
           "ground state detected correctly (drives Taxi-in / Arrived)")
else:
    report(isinstance(alt, (int, float)),
           f"alt_baro numeric ({alt!r}) -> airborne, altitude={state['altitude_ft']}")

report(state["speed_kts"] is not None, "ground speed present", fatal=False)
report(state["track"] is not None, "track/heading present", fatal=False)
report(state["registration"] is not None,
       f"tail number present ({state['registration']})", fatal=False)
report(state["type_code"] is not None,
       f"type code present ({state['type_code']} -> {state['aircraft_type']})",
       fatal=False)

unexpected = state["aircraft_type"] == state["type_code"] and state["type_code"]
if unexpected:
    print(f"  note  type code {state['type_code']!r} has no friendly name yet; "
          f"add it to TYPE_NAMES in app/airplaneslive.py to spell it out")

print()
if problems:
    print(f"{len(problems)} PROBLEM(S) — the adapter needs adjusting:")
    for p in problems:
        print(f"  - {p}")
    sys.exit(1)
print("Response shape matches what the app expects.")
