#!/usr/bin/env python3
"""Probe AeroAPI with a real key, on the real API.

Run this ON THE SERVER, before trusting the toggle:

    docker compose exec flight-tracker python3 check_aeroapi.py YOUR_KEY ENY3729 DFW OKC

It answers the two things that can't be checked from a development machine:
whether the data is actually any good for Envoy flights, and what a query
really costs. It makes exactly ONE query.

Pick a flight that is operating today. The route arguments matter — they
are how the right record is picked out when a flight number is used in
both directions.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import requests
except ImportError:
    print("requests not installed — run this inside the container")
    sys.exit(2)

from app.aeroapi import BASE_URL, pick_flight, normalize
from app.enrichment import derive_status, delay_info, gate_info

if len(sys.argv) < 5:
    print(__doc__)
    sys.exit(2)

key, ident, origin, destination = sys.argv[1], sys.argv[2].upper(), sys.argv[3].upper(), sys.argv[4].upper()
url = f"{BASE_URL}/flights/{ident}"
print(f"GET {url}\n(1 query against your key)\n")

try:
    r = requests.get(url, headers={"x-apikey": key}, params={"max_pages": 1}, timeout=20)
except Exception as e:
    print(f"NETWORK FAILURE: {e}")
    sys.exit(1)

print(f"HTTP {r.status_code}")
if r.status_code in (401, 403):
    print("Key rejected. Check it's an AeroAPI key and the account is active.")
    sys.exit(1)
if r.status_code == 429:
    print("Rate limited or out of quota.")
    sys.exit(1)
if r.status_code != 200:
    print(r.text[:400]); sys.exit(1)

data = r.json()
flights = data.get("flights") or []
print(f"records returned for {ident}: {len(flights)}")
for f in flights:
    o = (f.get("origin") or {}).get("code_iata")
    d = (f.get("destination") or {}).get("code_iata")
    print(f"   {o}-{d}  sched_out={f.get('scheduled_out')}  "
          f"cancelled={f.get('cancelled')} diverted={f.get('diverted')}")

match = pick_flight(flights, origin, destination, None)
if not match:
    print(f"\nNo record matched {origin}-{destination}. If the flight isn't "
          f"operating today, try one that is.")
    sys.exit(0)

enr = normalize(match)
print(f"\n--- matched {origin}-{destination} ---")
for k in ("fa_flight_id", "registration", "cancelled", "diverted", "status_text"):
    print(f"  {k:16} {enr.get(k)!r}")

print("\n--- OOOI (the reason to pay for this) ---")
for stage in ("out", "off", "on", "in"):
    print(f"  {stage.upper():4} sched={enr.get('scheduled_'+stage)}  "
          f"est={enr.get('estimated_'+stage)}  actual={enr.get('actual_'+stage)}")

print("\n--- what the app will show ---")
print(f"  status : {derive_status(enr, None)}")
print(f"  delay  : {delay_info(enr)}")
print(f"  gates  : {gate_info(enr)}")

print("\n--- COVERAGE CHECK ---")
missing = [k for k in ("actual_out", "actual_off", "actual_on", "actual_in",
                       "gate_origin", "gate_destination") if not enr.get(k)]
if missing:
    print(f"  not populated right now: {', '.join(missing)}")
    print("  (times legitimately absent before the event happens; gates absent")
    print("   for a whole airline is the thing worth knowing)")
else:
    print("  all OOOI and gate fields populated")

print("\nCheck the actual cost of this query at "
      "https://www.flightaware.com/aeroapi/portal — that number decides")
print("whether the 4-6 queries per flight budget fits the free credit.")
