"""Regression cover for the v5 rebuild: the flight row, the two tags, and
the closure guards the pilot caught.

Runs against a scratch database via PT_DB_FILE, so it never touches the
real one and suites can't leak state into each other.
"""
import os
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone

os.environ["PT_DB_FILE"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["TRACK_POLLER_ENABLED"] = "0"

from app.db import init_db, get_connection            # noqa: E402
from app import tags, closure                          # noqa: E402
from app.airports import enrich_leg                    # noqa: E402
from app.flights import (get_flight, replace_schedule, write, load_schedule,  # noqa: E402
                         purge_old, flight_key, owners_of)
from app.models import FlightLeg                       # noqa: E402
from app.view import build, recompute_derived          # noqa: E402

init_db()
UTC = timezone.utc
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail and not cond else ""))


def make_user(username="pilot"):
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
            (username, "x", datetime.now(UTC).isoformat()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def make_leg(leg_id="2026-08-04-3729-DFW-OKC", origin="DFW", destination="OKC",
             dep=time(14, 0), arr=time(15, 10), on=date(2026, 8, 4)):
    leg = FlightLeg(id=leg_id, date=on, flight_number="3729", origin=origin,
                    destination=destination, dep_time_local=dep, arr_time_local=arr)
    enrich_leg(leg)
    return leg


UID = make_user()
LEG = make_leg()
replace_schedule(UID, [LEG])
DEP = LEG.dep_datetime_utc()
ARR = LEG.arr_datetime_utc()


print("\n-- write modes --")
write(LEG.id, once={"off_observed": "2026-08-04T19:10:00+00:00"})
write(LEG.id, once={"off_observed": "2026-08-04T19:40:00+00:00"})
check("ONCE keeps the first value",
      get_flight(LEG.id)["off_observed"].startswith("2026-08-04T19:10"))

write(LEG.id, latest={"last_speed_kts": 420})
write(LEG.id, latest={"last_speed_kts": None})
check("LATEST: a blank never overwrites a known value",
      get_flight(LEG.id)["last_speed_kts"] == 420)
write(LEG.id, latest={"last_speed_kts": 380})
check("LATEST: a real value does overwrite",
      get_flight(LEG.id)["last_speed_kts"] == 380)

write(LEG.id, always={"progress_pct": 44.0})
write(LEG.id, always={"progress_pct": None})
check("ALWAYS overwrites even with nothing",
      get_flight(LEG.id)["progress_pct"] is None)


print("\n-- schedule re-import keeps observed data --")
replace_schedule(UID, [LEG])
check("re-pasting the same schedule keeps the observed wheels-off",
      get_flight(LEG.id)["off_observed"] is not None)
check("re-pasting keeps the leg", len(load_schedule(UID)) == 1)


print("\n-- phase ladder is forward-only --")
check("Scheduled -> In air advances",
      tags.advance_phase("Scheduled", "In air") == "In air")
check("In air -> Scheduled is REFUSED (coverage gap can't walk it back)",
      tags.advance_phase("In air", "Scheduled") == "In air")
check("Taxi-in -> In air is refused",
      tags.advance_phase("Taxi-in", "In air") == "Taxi-in")
check("Landing -> Taxi-in advances",
      tags.advance_phase("Landing", "Taxi-in") == "Taxi-in")


print("\n-- phase from the row --")
write(LEG.id, always={"last_on_ground": 0, "last_lat": 33.0, "last_lon": -97.5,
                           "airborne_seen": 1})
check("airborne far out reads In air",
      tags.compute_phase(get_flight(LEG.id), LEG, DEP) == tags.PHASE_IN_AIR)

d = LEG.dest_info
write(LEG.id, always={"last_lat": d.lat + 0.05, "last_lon": d.lon})
check("airborne inside 8nm reads Landing",
      tags.compute_phase(get_flight(LEG.id), LEG, DEP) == tags.PHASE_LANDING)

write(LEG.id, always={"last_on_ground": 1})
check("on the ground after flying reads Taxi-in",
      tags.compute_phase(get_flight(LEG.id), LEG, DEP) == tags.PHASE_TAXI_IN)

write(LEG.id, always={"last_on_ground": None, "last_lat": None, "last_lon": None,
                           "airborne_seen": 0, "off_actual_api": None,
                           "off_observed": None, "on_observed": None})
row = get_flight(LEG.id)
check("no signal at all does NOT produce a phase of its own",
      tags.compute_phase(row, LEG, DEP) == tags.PHASE_SCHEDULED)

# A leg with no ADS-B whatsoever still gets a phase from the airline.
write(LEG.id, always={"off_actual_api": (DEP + timedelta(minutes=12)).isoformat()})
check("airline wheels-off gives In air with zero ADS-B coverage",
      tags.compute_phase(get_flight(LEG.id), LEG, DEP) == tags.PHASE_IN_AIR)


print("\n-- the Delayed pill --")
write(LEG.id, always={
    "out_scheduled": DEP.isoformat(), "out_estimated": None,
    "out_actual_api": None, "off_actual_api": None, "status_tag": None})
status, dep_rev, _ = tags.compute_status(get_flight(LEG.id), LEG, DEP)
check("no revision means no pill", status is None)

# The airline publishes a schedule 5 min after the FFDO time but has not
# moved anything. This is the false positive the pilot asked to kill.
write(LEG.id, always={"out_scheduled": (DEP + timedelta(minutes=5)).isoformat(),
                           "out_estimated": (DEP + timedelta(minutes=5)).isoformat()})
status, _, _ = tags.compute_status(get_flight(LEG.id), LEG, DEP)
check("airline schedule differing from FFDO alone is NOT Delayed", status is None)

write(LEG.id, always={"out_estimated": (DEP + timedelta(minutes=40)).isoformat()})
status, dep_rev, _ = tags.compute_status(get_flight(LEG.id), LEG, DEP)
check("a real push past the FFDO time IS Delayed", status == tags.STATUS_DELAYED)
check("the push is measured against the FFDO time", dep_rev == 40, f"got {dep_rev}")

write(LEG.id, always={"out_estimated": DEP.isoformat()})
status, _, _ = tags.compute_status(get_flight(LEG.id), LEG, DEP)
check("pulling the time back clears the pill (status moves both ways)", status is None)

# Out 12 min late, but the airline never moved anything: no revision to
# `out_estimated`, so no pill — while the note stays honest about the clock.
#
# FIXTURE CORRECTED (1.21.0). It used to set `out_scheduled` to the ACTUAL
# time as well, which the lateness note ignored while it measured against
# the FFDO. The note now prefers the airline's published time (see
# view._baseline), so that fixture asserts two things that cannot both be
# true: the airline planned this flight for 12:12 AND it went 12 minutes
# late. `out_scheduled` is a snapshot of the ORIGINAL published time —
# enrichment.py writes it once and never again, and a delay moves
# `out_estimated`, not this — so the realistic value is the original.
write(LEG.id, always={"out_estimated": None,
                           "out_actual_api": (DEP + timedelta(minutes=12)).isoformat(),
                           "out_scheduled": DEP.isoformat()})
status, _, _ = tags.compute_status(get_flight(LEG.id), LEG, DEP)
check("out 12 min late with no airline push shows no Delayed pill", status is None)
v = build(get_flight(LEG.id), LEG, ARR, "24")
check("...but the lateness note still says 12 min late",
      v["dep_delay"] and v["dep_delay"]["minutes"] == 12, str(v.get("dep_delay")))

write(LEG.id, always={"cancelled": 1})
status, _, _ = tags.compute_status(get_flight(LEG.id), LEG, DEP)
check("cancelled outranks everything", status == tags.STATUS_CANCELLED)
v = build(get_flight(LEG.id), LEG, ARR, "24")
check("cancelled hides the phase pill", v["phase_tag"] is None)
write(LEG.id, always={"cancelled": 0, "status_tag": tags.STATUS_CANCELLED})
status, _, _ = tags.compute_status(get_flight(LEG.id), LEG, DEP)
check("Cancelled sticks even if a later record omits it",
      status == tags.STATUS_CANCELLED)
write(LEG.id, always={"status_tag": None})


print("\n-- closure: the delayed-flight backstop bug --")
L2 = make_leg("2026-08-04-3730-DFW-MFE", destination="MFE")
replace_schedule(UID, [LEG, L2])
D2, A2 = L2.dep_datetime_utc(), L2.arr_datetime_utc()

# Four hours past scheduled arrival, never departed, never any signal.
late = A2 + timedelta(hours=4)
row = get_flight(L2.id)
check("a flight that never departed cannot be closed by the backstop",
      closure.decide(row, L2, late, signal_gap=None) is None)

# Same flight, now demonstrably airborne, but only just.
write(L2.id, always={"airborne_seen": 1,
                          "off_observed": (late - timedelta(minutes=30)).isoformat()})
row = get_flight(L2.id)
check("once airborne, the backstop anchors on the ACTUAL departure, not the plan",
      closure.decide(row, L2, late, signal_gap=99999) is None)
much_later = late + timedelta(hours=4)
check("...and does fire once the projected arrival is 3h past",
      (closure.decide(row, L2, much_later, signal_gap=99999) or (None,))[0]
      == closure.SOURCE_BACKSTOP)


print("\n-- closure: observed arrival needs a landing first --")
L3 = make_leg("2026-08-04-3731-DFW-ABI", destination="ABI")
replace_schedule(UID, [LEG, L2, L3])
stopped_at = L3.dep_datetime_utc() - timedelta(hours=1)
# Parked at the departure gate, transponder off. Stationary and silent,
# but it has never flown.
write(L3.id, always={"stopped_since": stopped_at.isoformat(),
                          "airborne_seen": 0, "landed_seen": 0})
row = get_flight(L3.id)
check("stationary + silent at the DEPARTURE gate is not an arrival",
      closure.decide(row, L3, stopped_at + timedelta(hours=1),
                     observed_in=stopped_at, stopped_for=3600,
                     signal_gap=3600) is None)

write(L3.id, always={"airborne_seen": 1, "landed_seen": 1,
                          "off_observed": (stopped_at - timedelta(hours=2)).isoformat()})
row = get_flight(L3.id)
verdict = closure.decide(row, L3, stopped_at + timedelta(minutes=20),
                         observed_in=stopped_at, stopped_for=1200, signal_gap=1200)
check("after a confirmed landing, stopped + silent DOES close it",
      verdict and verdict[0] == closure.SOURCE_OBSERVED)

verdict = closure.decide(row, L3, stopped_at + timedelta(minutes=20),
                         observed_in=stopped_at, stopped_for=1200, signal_gap=60)
check("stopped but still transmitting is not an arrival (waiting for a stand)",
      verdict is None)


print("\n-- closure: airline gate-in upgrades an observed close --")
closure.close(L3, get_flight(L3.id), closure.SOURCE_OBSERVED,
              stopped_at, stopped_at)
check("observed close is recorded", get_flight(L3.id)["closed_by"] == "observed")
gate_in = (stopped_at + timedelta(minutes=4)).isoformat()
write(L3.id, always={"in_actual_api": gate_in})
closure.maybe_close(L3, get_flight(L3.id), stopped_at + timedelta(hours=1))
row = get_flight(L3.id)
check("a late airline gate-in upgrades it", row["closed_by"] == "airline")
check("...and takes the airline's time", row["closed_at"].startswith(gate_in[:16]))
check("arrival source is recorded as airline", row["arrival_source"] == "airline")


print("\n-- every figure comes from a position fix, or does not exist --")
write(LEG.id, always={"last_lat": None, "last_lon": None, "last_on_ground": None,
                           "phase_tag": tags.PHASE_SCHEDULED, "off_actual_api": None,
                           "last_signal_at": None, "cancelled": 0})
derived = recompute_derived(get_flight(LEG.id), LEG, ARR + timedelta(hours=1))
check("a flight still at the gate has NO percentage, not 0 and not 100",
      derived["progress_pct"] is None, str(derived["progress_pct"]))
check("no live fix means no distance", derived["distance_nm"] is None)
check("no live fix means no countdown either", derived["ete_min"] is None,
      str(derived["ete_min"]))

# The failure this replaced: on a coverage hole mid-cruise the percentage
# and distance correctly vanished while ETE kept ticking down against the
# airline's revised arrival, so one figure on the card contradicted the two
# blanks beside it.
write(LEG.id, always={"last_lat": None, "last_lon": None,
                           "phase_tag": tags.PHASE_IN_AIR,
                           "in_estimated": (ARR + timedelta(minutes=40)).isoformat()})
derived = recompute_derived(get_flight(LEG.id), LEG, ARR)
check("a revised arrival cannot manufacture an ETE",
      derived["ete_min"] is None, str(derived["ete_min"]))

o = LEG.origin_info
mid_lat = (o.lat + d.lat) / 2
mid_lon = (o.lon + d.lon) / 2
write(LEG.id, always={"last_lat": mid_lat, "last_lon": mid_lon,
                           "last_on_ground": 0, "phase_tag": tags.PHASE_IN_AIR,
                           "last_signal_at": ARR.isoformat()})
derived = recompute_derived(get_flight(LEG.id), LEG, ARR)
check("halfway along the route reads about 50%",
      derived["progress_pct"] and 40 < derived["progress_pct"] < 60,
      str(derived["progress_pct"]))
check("...and a real fix does yield a distance", derived["distance_nm"] is not None)


print("\n-- signal note replaces the Unknown phase --")
row = get_flight(LEG.id)
check("fresh signal produces no note", tags.signal_note(row, ARR) is None)
check("a 14 minute gap says so",
      (tags.signal_note(row, ARR + timedelta(minutes=14)) or "").startswith("no signal for 14"))


print("\n-- two crew on one flight --")
UID2 = make_user("firstofficer")
# The FO imports the same leg. Same date, flight number, origin and dest,
# so the same id — he joins the existing row rather than making his own.
replace_schedule(UID2, [make_leg()])
check("both crew are on the flight", sorted(owners_of(LEG.id)) == sorted([UID, UID2]),
      str(owners_of(LEG.id)))
check("there is exactly ONE row for the flight",
      len([r for r in __import__("app.db", fromlist=["get_connection"])
           .get_connection().execute("SELECT id FROM flights WHERE id = ?", (LEG.id,))]) == 1)
write(LEG.id, always={"gate_destination": "A17"})
check("what one crew member's key paid for, both see",
      get_flight(LEG.id)["gate_destination"] == "A17")
check("the FO sees the leg on his schedule",
      LEG.id in {l.id for l in load_schedule(UID2)})

# A deadhead for one pilot is a working leg for the other. Same aeroplane.
# The flag lives on ROSTER, not on flights, which is the point here.
#
# ON A LEG NOT YET FLOWN. It used to use the shared fixture leg, which is
# dated in the past — and since 1.22.0 a flown leg is frozen against
# imports outright, flag included, so that version of this test was
# asserting the per-person rule and the absence of the freeze at once.
FUTURE_DH = date.today() + timedelta(days=30)
dh_id = f"{FUTURE_DH.isoformat()}-3729-DFW-OKC"
dh = make_leg(leg_id=dh_id, on=FUTURE_DH)
dh.is_deadhead = True
plain = make_leg(leg_id=dh_id, on=FUTURE_DH)
replace_schedule(UID, [LEG, plain])
replace_schedule(UID2, [make_leg(), dh])
def _dh_of(uid_):
    return {l.id: l.is_deadhead for l in load_schedule(uid_)}.get(dh_id)
check("deadheading is per-person, not per-flight",
      _dh_of(UID2) is True and _dh_of(UID) is False,
      f"UID2={_dh_of(UID2)} UID={_dh_of(UID)}")

# AND THE FREEZE (1.22.0). Re-importing a leg that has already been flown
# changes nothing about it — not the times, not this flag. The import
# still decides WHETHER a flown leg is yours (the review page can remove
# it); it has no say in what that leg WAS.
before_flags = {l.id: l.is_deadhead for l in load_schedule(UID2)}
flown_flipped = make_leg()
flown_flipped.is_deadhead = not before_flags[LEG.id]
replace_schedule(UID2, [flown_flipped, dh])
check("a flown leg's deadhead flag is frozen against re-import",
      {l.id: l.is_deadhead for l in load_schedule(UID2)}[LEG.id] == before_flags[LEG.id],
      str({l.id: l.is_deadhead for l in load_schedule(UID2)}))

# One pilot dropping the leg must not delete the other's flight.
from app.flights import delete_leg                                   # noqa: E402
delete_leg(UID2, LEG.id)
check("removing it from one schedule leaves the flight intact",
      get_flight(LEG.id) is not None)
check("...and off that pilot's schedule only",
      LEG.id not in {l.id for l in load_schedule(UID2)}
      and LEG.id in {l.id for l in load_schedule(UID)})

print("\n-- flight key --")
check("a deadhead shares the working leg's track",
      flight_key("2026-08-04-3729-DFW-OKC-DH") == "2026-08-04-3729-DFW-OKC")


print("\n-- retention --")
old = make_leg("2020-01-01-1111-DFW-OKC", on=date(2020, 1, 1))
replace_schedule(UID, [LEG, L2, L3, old])
replace_schedule(UID2, [])
removed = purge_old()
ids = {l.id for l in load_schedule(UID)}
check("a leg older than 30 days is purged", old.id not in ids, str(ids))
check("current legs survive the purge", LEG.id in ids)


print("\n-- a flight outlives everybody's schedule --")
# The owner's actual workflow: swap in a throwaway schedule to watch live
# traffic, then put the real bid line back. Through v5.6 the sweep deleted
# every un-rostered flight, so the real legs were gone before he swapped
# back and the re-import built blank rows from the timetable.
flown = make_leg("2026-08-04-4242-DFW-ICT", on=date(2026, 8, 4))
replace_schedule(UID, [flown])
write(flown.id, once={"gate_destination": "A17", "baggage_claim": "6",
                      "tail_api": "N600EN"})
write(flown.id, latest={"closed": 1, "closed_by": "airline"})
from app.track import record_position, get_breadcrumb                # noqa: E402
record_position(flown.id, 32.9, -97.0,
                datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc), on_ground=False)

test_sched = make_leg("2026-08-05-9999-LAX-JFK", on=date(2026, 8, 5))
replace_schedule(UID, [test_sched])
check("swapping schedules takes the leg off the roster",
      flown.id not in {l.id for l in load_schedule(UID)})

purge_old()
purge_old()   # the track used to die on the second sweep, not the first
row = get_flight(flown.id)
check("...but the FLIGHT survives with nobody rostered on it", row is not None)
check("...keeping its arrival gate", row is not None and row["gate_destination"] == "A17")
check("...its baggage belt", row is not None and row["baggage_claim"] == "6")
check("...its aircraft", row is not None and row["tail_api"] == "N600EN")
check("...and its track", len(get_breadcrumb(flown.id)) > 0)

replace_schedule(UID, [flown])
back = get_flight(flown.id)
check("re-adding the schedule adopts the old row, not a blank one",
      back is not None and back["gate_destination"] == "A17")
check("...and the closeout record comes back with it",
      back is not None and back["closed_by"] == "airline")

# The FO case: a different pilot imports a trip they were on and inherits
# everything already known about it.
replace_schedule(UID2, [flown])
check("another pilot importing the same leg adopts it too",
      flown.id in {l.id for l in load_schedule(UID2)}
      and get_flight(flown.id)["gate_destination"] == "A17")

# Retention still has the last word.
ancient = make_leg("2019-05-05-1212-DFW-OKC", on=date(2019, 5, 5))
replace_schedule(UID2, [flown, ancient])
replace_schedule(UID2, [flown])          # un-roster it, as a swap would
purge_old()
check("an un-rostered flight past 30 days is still purged",
      get_flight(ancient.id) is None)


# -- the taxi-in trap (1.4.0) ------------------------------------------------
print("\nA parked aircraft that keeps transmitting must still close")
from app.closure import decide, STOPPED_LONG, SOURCE_OBSERVED
from datetime import timezone as _tz, timedelta as _td
from datetime import datetime as _dt


class _Leg:
    id = "L"
    flight_number = "1"
    date = _dt.now(_tz.utc).date()

    def dep_datetime_utc(self):
        return None

    def arr_datetime_utc(self):
        return None


class _Row(dict):
    def __getitem__(self, k):
        return self.get(k)


_now = _dt(2026, 6, 15, 20, 0, tzinfo=_tz.utc)
_landed = _Row(airborne_seen=1, landed_seen=1, relaunched=0)

# THE BUG. Both the observed route and the backstop required the transponder
# to go quiet. An aircraft parked at the gate with its transponder still on --
# ordinary behaviour, especially with the APU running -- satisfied neither, so
# the leg never closed and the next leg never became current. From outside,
# the app looked frozen on a flight that had plainly finished.
_v = decide(_landed, _Leg(), _now, observed_in=_now - _td(minutes=45),
            stopped_for=45 * 60, signal_gap=30, enrichment_fresh=False)
check("a 45-minute stop closes even while transmitting",
      _v is not None and _v[0] == SOURCE_OBSERVED)

# The five-minute rule still needs silence: a short stop is as likely to be
# holding for a gate as parked at one.
_v = decide(_landed, _Leg(), _now, observed_in=_now - _td(minutes=7),
            stopped_for=7 * 60, signal_gap=30, enrichment_fresh=False)
check("a short stop while transmitting does NOT close", _v is None)

_v = decide(_landed, _Leg(), _now, observed_in=_now - _td(minutes=7),
            stopped_for=7 * 60, signal_gap=600, enrichment_fresh=False)
check("a short stop plus silence still closes",
      _v is not None and _v[0] == SOURCE_OBSERVED)

# The guard that stops a delayed flight closing itself at the gate must
# outrank the new route.
_v = decide(_Row(airborne_seen=0, landed_seen=0), _Leg(), _now,
            observed_in=_now - _td(minutes=45), stopped_for=45 * 60,
            signal_gap=30, enrichment_fresh=False)
check("a leg that never departed cannot close on a long stop", _v is None)
check("the long-stop threshold is longer than the short one",
      STOPPED_LONG.total_seconds() > 5 * 60)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1)
