"""Drives one flight through a whole trip with a fake ADS-B feed.

The unit suites check each rule in isolation. This checks that the poller
actually strings them together: acquire the aircraft, taxi out, climb,
cruise, lose coverage, come back, land, park, close out. Nothing here
touches the network — airplanes.live is replaced by a scripted feed.

The coverage-gap step is the point of the whole exercise. That is the
v4 bug the pilot reported: the phase fell to Unknown mid-cruise and the
card told his family the app had lost him.
"""
import os
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone

os.environ["PT_DB_FILE"] = os.path.join(tempfile.mkdtemp(), "e2e.db")
os.environ["TRACK_POLLER_ENABLED"] = "0"

from app.db import init_db                             # noqa: E402
from app.auth import create_user                       # noqa: E402
from app.airports import enrich_leg                    # noqa: E402
from app.models import FlightLeg                       # noqa: E402
from app.flights import get_row, save_schedule         # noqa: E402
from app.track import get_breadcrumb                   # noqa: E402
from app import livesource, poller, tags               # noqa: E402

init_db()
UTC = timezone.utc
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


UID = create_user("e2e", "pw12345678")
TODAY = datetime.now(UTC).date()
LEG = FlightLeg(id=f"{TODAY}-3729-DFW-OKC", date=TODAY,
                flight_number="3729", origin="DFW", destination="OKC",
                dep_time_local=time(14, 0), arr_time_local=time(15, 10))
enrich_leg(LEG)
save_schedule(UID, [LEG])
O, D = LEG.origin_info, LEG.dest_info
DEP = LEG.dep_datetime_utc()

# The scripted feed. `_now` is set by each step before poll_once() runs.
_state = {"value": None}
_now = {"value": DEP}
# poller does `from .livesource import live_state`, which binds the name at
# import time — so the fake has to be installed on the poller's own module,
# not on livesource.
poller.live_state = lambda callsign, cache_ttl_s=8.0: _state["value"]


def fix(lat, lon, on_ground, speed, alt=None, squawk="1200"):
    return {"callsign": "ENY3729", "icao24": "a1b2c3", "lat": lat, "lon": lon,
            "on_ground": on_ground, "altitude_ft": alt, "speed_kts": speed,
            "track": 0.0, "registration": "N204NN", "type_code": "E75L",
            "aircraft_type": "Embraer 175", "squawk": squawk,
            "position_age_s": 2.0, "source": "test"}


def step(label, minutes_from_dep, state):
    """One sweep at a chosen instant.

    poll_once takes the clock as an argument, so the whole sweep — leg
    selection, tags, closure — runs at exactly one time. That is also why
    it takes an argument in production: two clocks in one sweep meant the
    leg could be chosen at one instant and judged at another.
    """
    _state["value"] = state
    at = DEP + timedelta(minutes=minutes_from_dep)
    poller.poll_once(at)
    row = get_row(UID, LEG.id)
    print(f"  T{minutes_from_dep:+04d}  {label:<22} "
          f"phase={row['phase_tag']!s:<10} status={row['status_tag']!s:<8} "
          f"closed={bool(row['closed'])}")
    return row


print("\n-- flying the leg --")
row = step("at the gate", -25, fix(O.lat, O.lon, True, 0))
check("aircraft acquired at the origin", row["aircraft_hex"] == "a1b2c3")
check("tail number arrives with the position", row["tail_adsb"] == "N204NN")
check("phase is Scheduled before pushback", row["phase_tag"] == tags.PHASE_SCHEDULED)

row = step("taxiing out", 4, fix(O.lat + 0.01, O.lon + 0.01, True, 18))
check("moving on the ground reads Taxi-out", row["phase_tag"] == tags.PHASE_TAXI_OUT)

row = step("airborne", 12, fix(O.lat + 0.3, O.lon + 0.3, False, 280, 11000))
check("off the ground reads In air", row["phase_tag"] == tags.PHASE_IN_AIR)
check("wheels-off observed and stamped", row["off_observed"] is not None)
check("progress is under way", 0 < (row["progress_pct"] or 0) < 100,
      str(row["progress_pct"]))

row = step("mid cruise", 25, fix((O.lat + D.lat) / 2, (O.lon + D.lon) / 2,
                                 False, 420, 24000))
mid_progress = row["progress_pct"]
check("about halfway", 40 < (mid_progress or 0) < 60, str(mid_progress))
check("distance to go is reported", (row["distance_nm"] or 0) > 0)

# THE BUG. No receiver, so the feed returns nothing at all.
row = step("no ADS-B coverage", 35, None)
check("a coverage gap does NOT walk the phase backwards",
      row["phase_tag"] == tags.PHASE_IN_AIR, str(row["phase_tag"]))
check("the last known position is not erased", row["last_lat"] is not None)
check("the tail number is not erased", row["tail_adsb"] == "N204NN")
note = tags.signal_note(row, DEP + timedelta(minutes=35))
check("...it says so instead", (note or "").startswith("no signal"), repr(note))

row = step("back in coverage", 55, fix(D.lat + 0.06, D.lon, False, 190, 3000))
check("inside 8nm reads Landing", row["phase_tag"] == tags.PHASE_LANDING)

row = step("rollout", 60, fix(D.lat, D.lon, True, 60))
row = step("touchdown confirmed", 62, fix(D.lat, D.lon, True, 22))
check("landing is confirmed only after it holds", bool(row["landed_seen"]))
check("on the ground after flying reads Taxi-in",
      row["phase_tag"] == tags.PHASE_TAXI_IN)

row = step("parked at the gate", 66, fix(D.lat, D.lon, True, 0))
check("stopping alone does not close the leg", not row["closed"])

# Blocked in: engines off, transponder off. The feed goes silent.
row = step("silent, 6 min stopped", 72, None)
check("stopped 6 min but silent only 6 does not close", not row["closed"])
row = step("silent, 12 min stopped", 78, None)
check("stopped and silent long enough DOES close", bool(row["closed"]))
check("closed on our own observation", row["closed_by"] == "observed")
check("phase reads Arrived", row["phase_tag"] == tags.PHASE_ARRIVED)
check("no status pill on a clean flight", row["status_tag"] is None)
check("no countdown once closed", row["ete_min"] is None)

print("\n-- the track --")
crumbs = get_breadcrumb(LEG.id)
check("a flown path was recorded", len(crumbs) >= 6, f"{len(crumbs)} points")
check("thinning collapsed the parked fixes", len(crumbs) <= 12, f"{len(crumbs)} points")

print("\n-- a closed leg is frozen --")
before = dict(get_row(UID, LEG.id))
row = step("something else on the callsign", 95,
           fix(O.lat, O.lon, False, 300, 15000, squawk="4321"))
check("the return flight cannot reopen it", bool(row["closed"]))
check("...and cannot move its position",
      row["last_lat"] == before["last_lat"], "the turn overwrote the leg")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1)
