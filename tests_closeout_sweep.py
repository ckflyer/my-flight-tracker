"""The leg that blocked in and never closed. (1.5.0)

THE BUG, as reported: an aircraft blocked in at 07:00 and the leg was
still open at 11:30. The 1.4.0 long-stop route was supposed to close it
after 30 stationary minutes and did not — not because the rule was wrong,
but because NOTHING WAS ASKING ANY MORE.

`poller.active_flights()` returns the CURRENT leg and imminent upcoming
ones. `schedule.get_current_info` releases a leg 3 hours past its
SCHEDULED arrival unless it is demonstrably still airborne. So every
closure route quietly expired at the same instant:

  * the BACKSTOP matures 3h after the REVISED arrival, which on a late
    flight is always later than 3h past the scheduled one;
  * `relaunch` needs a later sweep to see the aircraft fly again;
  * the LONG STOP needs a sweep at the 30-minute mark.

The old suites could not catch this because they drive `maybe_close`
directly, or drive `poll_once` over a 70-minute leg that finishes long
inside its window. Every test here runs through `poll_once` at instants
PAST the grace, which is the only place the bug lives.

Second half: the gate-in backfill. `maybe_close` has always been able to
upgrade a provisional close to the airline's own figure, and never could,
because a closed leg stopped being polled and `should_query` refuses to
spend on one. That is the value a logbook is allowed to use, so it is
worth going back for.
"""
import os
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone

os.environ["PT_DB_FILE"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["TRACK_POLLER_ENABLED"] = "0"

from app.db import init_db, get_connection              # noqa: E402
from app import closure, enrichment, poller             # noqa: E402
from app.airports import enrich_leg                     # noqa: E402
from app.flights import (flight_key, get_flight, replace_schedule,  # noqa: E402
                         rows_awaiting_gate_in, unclosed_rows, write)
from app.models import FlightLeg                        # noqa: E402
from app.schedule import CURRENT_GRACE                  # noqa: E402

init_db()
UTC = timezone.utc
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"  {detail}" if detail and not cond else ""))


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


TODAY = date.today()


def make_leg(num="3729", origin="DFW", destination="OKC",
             dep=time(5, 0), arr=time(6, 10), on=None):
    on = on or TODAY
    leg = FlightLeg(id=f"{on.isoformat()}-{num}-{origin}-{destination}", date=on,
                    flight_number=num, origin=origin, destination=destination,
                    dep_time_local=dep, arr_time_local=arr)
    enrich_leg(leg)
    return leg


UID = make_user()

# The poller must not reach out to anything during these tests. Both fakes
# are installed on the poller's own module because it does
# `from .livesource import live_state`, which binds the name at import.
poller.live_state = lambda callsign: None
enrichment.refresh_usage = lambda uid, now: False


# ---------------------------------------------------------------------------
print("\n-- the reported bug: blocked in, never closed --")

LEG = make_leg()
replace_schedule(UID, [LEG])
DEP, ARR = LEG.dep_datetime_utc(), LEG.arr_datetime_utc()

# Blocked in ten minutes after arrival and sat there. Exactly the 1.4.0
# long-stop shape: landed, stationary, and STILL TRANSMITTING, so no
# silence-based route can fire.
BLOCKED_IN = ARR + timedelta(minutes=10)
write(LEG.id, always={
    "airborne_seen": 1, "landed_seen": 1,
    "off_observed": (DEP + timedelta(minutes=12)).isoformat(),
    "on_observed": ARR.isoformat(),
    "stopped_since": BLOCKED_IN.isoformat(),
    # Transponder still alive, updated right up to "now" on each sweep.
    "last_signal_at": BLOCKED_IN.isoformat(),
})

# T+45min past arrival: still inside the live window, closes normally.
row = get_flight(LEG.id)
check("still inside the window at T+45", not row["closed"])

# Now jump to 4.5 hours past arrival — past CURRENT_GRACE, which is where
# the old build abandoned the leg. This is the reported 07:00 -> 11:30.
LATE = ARR + timedelta(hours=4, minutes=30)
write(LEG.id, always={"last_signal_at": LATE.isoformat()})  # still transmitting

check("the leg is past the live window",
      LATE > ARR + CURRENT_GRACE)
check("...so nothing in the old sweep would see it",
      LEG.id not in poller.active_flights(LATE))
check("...but the closeout sweep does",
      any(r["id"] == flight_key(LEG.id) for r in unclosed_rows(LATE)))

poller.poll_once(LATE)
row = get_flight(LEG.id)
check("THE BUG: the abandoned leg now closes", bool(row["closed"]),
      f"closed_by={row['closed_by']}")
check("...on the long stop, not the backstop", row["closed_by"] == "observed",
      str(row["closed_by"]))
check("...at the observed block-in time",
      str(row["in_observed"] or "").startswith(BLOCKED_IN.isoformat()[:16]))
check("...and the phase pill agrees", row["phase_tag"] == "Arrived",
      str(row["phase_tag"]))


# ---------------------------------------------------------------------------
print("\n-- the backstop can reach its own maturity now --")

L2 = make_leg(num="3566", origin="DFW", destination="ICT")
replace_schedule(UID, [LEG, L2])
D2, A2 = L2.dep_datetime_utc(), L2.arr_datetime_utc()

# Departed for real, then total loss of coverage. Nothing was ever seen to
# land, so no observed route can fire — the backstop is the only exit, and
# it matures 3h after the REVISED arrival.
write(L2.id, always={
    "airborne_seen": 1,
    "off_observed": (D2 + timedelta(minutes=10)).isoformat(),
    "in_estimated": (A2 + timedelta(hours=1)).isoformat(),   # ran an hour late
    "last_signal_at": (D2 + timedelta(minutes=40)).isoformat(),
})

# 3h05 past the SCHEDULED arrival: the old build had already let go of the
# leg here, while the backstop was not due for another hour.
TOO_EARLY = A2 + timedelta(hours=3, minutes=5)
poller.poll_once(TOO_EARLY)
check("a late leg does not close before its backstop matures",
      not get_flight(L2.id)["closed"])

# 4h05: 3h past the REVISED arrival. Unreachable before 1.5.0.
MATURE = A2 + timedelta(hours=4, minutes=5)
poller.poll_once(MATURE)
row = get_flight(L2.id)
check("the backstop fires once it is genuinely due", bool(row["closed"]),
      f"closed_by={row['closed_by']}")
check("...recorded as backstop", row["closed_by"] == "backstop",
      str(row["closed_by"]))


# ---------------------------------------------------------------------------
print("\n-- what the sweep must NOT do --")

L3 = make_leg(num="3900", origin="DFW", destination="TUL")
replace_schedule(UID, [LEG, L2, L3])
A3 = L3.arr_datetime_utc()
# Never departed: no ADS-B, no airline gate-out, nothing. has_departed is
# the guard the pilot found in v5.0, and the new sweep must not weaken it.
poller.poll_once(A3 + timedelta(hours=6))
check("a leg with no evidence of departure still cannot close",
      not get_flight(L3.id)["closed"])

# A leg older than the sweep window is left alone rather than re-judged
# every 20 seconds forever.
OLD = make_leg(num="3111", origin="DFW", destination="SGF",
               on=TODAY - timedelta(days=30))
replace_schedule(UID, [LEG, L2, L3, OLD])
write(OLD.id, always={"airborne_seen": 1})
check("a month-old unresolved leg is outside the sweep window",
      not any(r["id"] == flight_key(OLD.id)
              for r in unclosed_rows(datetime.now(UTC))))

# An un-rostered flight is nobody's leg and is never swept.
conn = get_connection()
conn.execute("DELETE FROM roster WHERE flight_id = ?", (flight_key(L3.id),))
conn.commit()
conn.close()
check("an un-rostered flight is not swept",
      not any(r["id"] == flight_key(L3.id)
              for r in unclosed_rows(A3 + timedelta(hours=6))))
replace_schedule(UID, [LEG, L2, L3, OLD])


# ---------------------------------------------------------------------------
print("\n-- the late gate-in chase --")

row = get_flight(LEG.id)
CLOSED_AT = datetime.fromisoformat(row["closed_at"])

check("a leg closed without gate-in is waiting for one",
      any(r["id"] == flight_key(LEG.id)
          for r in rows_awaiting_gate_in(CLOSED_AT + timedelta(hours=2))))
check("...but not before the first gap has passed",
      enrichment.should_backfill_gate_in(
          get_flight(LEG.id), CLOSED_AT + timedelta(minutes=30)) is None)
check("...and is due at +90 minutes",
      enrichment.should_backfill_gate_in(
          get_flight(LEG.id), CLOSED_AT + timedelta(minutes=95)) is not None)

# Count the paid attempts across a great many sweeps. The carrier.py
# lesson: an attempt that records nothing gets repeated forever.
CALLS = {"n": 0}
GATE_IN = ARR + timedelta(minutes=14)


def fake_fetch(api_key, callsign, origin, dest, dep_utc, want_raw=False):
    CALLS["n"] += 1
    if CALLS["n"] == 1:
        return None, None, 1          # airline still has nothing
    return ({"actual_in": GATE_IN.isoformat()}, {"actual_in": GATE_IN.isoformat()}, 1)


enrichment.fetch_leg = fake_fetch
conn = get_connection()
conn.execute("UPDATE users SET aeroapi_enabled = 1, aeroapi_key = 'k', "
             "aeroapi_budget = 5.0 WHERE id = ?", (UID,))
conn.commit()
conn.close()

# Sweep every 20 seconds for 30 hours. Only the scheduled attempts may bill.
t = CLOSED_AT
for _ in range(int(30 * 3600 / 20)):
    t += timedelta(seconds=20)
    poller._gate_in_sweep(t)

row = get_flight(LEG.id)
check("the first attempt found nothing and was recorded", CALLS["n"] >= 1)
check("the second attempt landed the airline gate-in",
      bool(row["in_actual_api"]), str(row["in_actual_api"]))
check("THE UPGRADE PATH IS REACHABLE AT LAST",
      row["closed_by"] == "airline", str(row["closed_by"]))
check("...and the arrival now cites the airline",
      row["arrival_source"] == "airline", str(row["arrival_source"]))
check("the chase is capped, not repeated forever",
      CALLS["n"] <= enrichment.GATEIN_BACKFILL_TRIES,
      f"{CALLS['n']} calls over 5,400 sweeps")
check("...and it stops once gate-in is known",
      enrichment.should_backfill_gate_in(row, t + timedelta(days=1)) is None)
check("a leg with gate-in is no longer waiting",
      not any(r["id"] == flight_key(LEG.id) for r in rows_awaiting_gate_in(t)))

# The hard cap, on its own leg, where the airline never answers at all.
# This is the expensive failure mode: a question with no answer, asked on
# every sweep. It must cost a fixed, tiny number of queries and then stop.
L5 = make_leg(num="3444", origin="DFW", destination="MSN")
replace_schedule(UID, [LEG, L2, L3, OLD, L5])
C5 = ARR + timedelta(hours=5)
write(L5.id, always={"airborne_seen": 1, "closed": 1, "closed_by": "backstop",
                     "closed_at": C5.isoformat()})

NEVER = {"n": 0}


def counting_fetch(*a, **k):
    NEVER["n"] += 1
    return None, None, 1


enrichment.fetch_leg = counting_fetch
t = C5
for _ in range(int(72 * 3600 / 20)):     # three days of sweeps, every 20s
    t += timedelta(seconds=20)
    poller._gate_in_sweep(t)
check("a silent airline costs exactly the capped number of queries",
      NEVER["n"] == enrichment.GATEIN_BACKFILL_TRIES,
      f"{NEVER['n']} over 12,960 sweeps vs cap {enrichment.GATEIN_BACKFILL_TRIES}")
check("...and the provisional close is left standing",
      get_flight(L5.id)["closed_by"] == "backstop",
      str(get_flight(L5.id)["closed_by"]))
check("...at a cost of under two cents",
      NEVER["n"] * enrichment.COST_PER_QUERY_USD < 0.02)

# A cancelled leg has no gate-in to find, and an airline-closed one
# already has it. Neither may ever be chased.
L4 = make_leg(num="3222", origin="DFW", destination="LIT")
replace_schedule(UID, [LEG, L2, L3, OLD, L4])
write(L4.id, always={"cancelled": 1, "closed": 1, "closed_by": "cancelled",
                     "closed_at": CLOSED_AT.isoformat()})
check("a cancelled leg is never chased",
      enrichment.should_backfill_gate_in(
          get_flight(L4.id), CLOSED_AT + timedelta(days=1)) is None)
write(L4.id, always={"cancelled": 0, "closed_by": "airline"})
check("a leg closed BY the airline is never chased",
      enrichment.should_backfill_gate_in(
          get_flight(L4.id), CLOSED_AT + timedelta(days=1)) is None)

check("the backfill counter is separate from the leg's ticket allowance",
      "gatein_tries" in {c[0] for c in __import__(
          "app.db", fromlist=["FLIGHT_COLUMNS"]).FLIGHT_COLUMNS})


print("\n-- the handover: a leg on the ground lets the next one through --")

# Closing the leg was only half the problem. Selection never asked whether
# a leg had FINISHED — only whether it had STARTED, and a finished leg has
# still started. So leg 1 held the card for the full three-hour grace while
# the crew were already boarding leg 2.
from app.schedule import get_current_info               # noqa: E402

H_UID = make_user("handover")
H1 = make_leg(num="5001", origin="DFW", destination="OKC",
              dep=time(8, 0), arr=time(9, 10))
H2 = make_leg(num="5002", origin="OKC", destination="DFW",
              dep=time(10, 30), arr=time(11, 40))
H3 = make_leg(num="5003", origin="DFW", destination="TUL",
              dep=time(13, 0), arr=time(14, 0))
replace_schedule(H_UID, [H1, H2, H3])
H1A, H2D, H2A, H3D = (H1.arr_datetime_utc(), H2.dep_datetime_utc(),
                      H2.arr_datetime_utc(), H3.dep_datetime_utc())

# Leg 1 is airborne. It must keep the card even once leg 2's window opens.
write(H1.id, always={"airborne_seen": 1, "last_on_ground": 0,
                     "off_observed": H1.dep_datetime_utc().isoformat()})
cur = get_current_info(H_UID, H2D - timedelta(minutes=15)).current
check("an AIRBORNE leg 1 keeps the card once leg 2's window opens",
      cur is not None and cur.flight_number == "5001",
      str(cur and cur.flight_number))

# Now it lands. Leg 2's window is not open yet, so leg 1 still has the card.
write(H1.id, always={"landed_seen": 1, "last_on_ground": 1})
cur = get_current_info(H_UID, H1A + timedelta(minutes=20)).current
check("a landed leg 1 keeps the card while leg 2 is still upcoming",
      cur is not None and cur.flight_number == "5001",
      str(cur and cur.flight_number))

# Leg 2's window opens (T-20). THE FIX.
cur = get_current_info(H_UID, H2D - timedelta(minutes=15)).current
check("THE HANDOVER: a leg on the ground cycles to the next flight",
      cur is not None and cur.flight_number == "5002",
      str(cur and cur.flight_number))
check("...and leg 1 goes to past, not nowhere",
      any(l.flight_number == "5001"
          for l in get_current_info(H_UID, H2D - timedelta(minutes=15)).past))

# It hands over ONE leg at a time, not straight to the end of the day.
write(H2.id, always={"airborne_seen": 1, "landed_seen": 1, "last_on_ground": 1,
                     "off_observed": H2D.isoformat()})
cur = get_current_info(H_UID, H3D - timedelta(minutes=15)).current
check("...one leg at a time, not straight to the last of the day",
      cur is not None and cur.flight_number == "5003",
      str(cur and cur.flight_number))

# The six signals. Each on its own must be enough, because they fail in
# different places: a station with no ground coverage never confirms a
# touchdown, and a leg with no ADS-B at all only ever has airline times.
# NOTE: a fresh flight NUMBER per case, not just a fresh user. Flight rows
# are SHARED by id, so reusing 6001 across cases would let one case's
# landed_seen leak into the next and every assertion would pass for the
# wrong reason.
for n, (col, label) in enumerate([("landed_seen", "an observed touchdown"),
                                  ("on_actual_api", "the airline's wheels-on"),
                                  ("in_actual_api", "the airline's gate-in"),
                                  ("in_observed", "an observed block-in"),
                                  ("closed", "the leg being closed")]):
    U = make_user(f"sig_{col}")
    A = make_leg(num=f"70{n}1", origin="DFW", destination="OKC",
                 dep=time(8, 0), arr=time(9, 10))
    B = make_leg(num=f"70{n}2", origin="OKC", destination="DFW",
                 dep=time(10, 30), arr=time(11, 40))
    replace_schedule(U, [A, B])
    val = 1 if col in ("landed_seen", "closed") else A.arr_datetime_utc().isoformat()
    write(A.id, always={"airborne_seen": 1,
                        "off_observed": A.dep_datetime_utc().isoformat(),
                        col: val})
    cur = get_current_info(U, B.dep_datetime_utc() - timedelta(minutes=15)).current
    check(f"{label} alone hands the card over",
          cur is not None and cur.flight_number == f"70{n}2",
          str(cur and cur.flight_number))

# And the one that matters at an outstation: airborne, now on the ground,
# but the touchdown confirmation timer never got the coverage to fire.
U = make_user("sig_lastground")
A = make_leg(num="7101", origin="DFW", destination="OKC",
             dep=time(8, 0), arr=time(9, 10))
B = make_leg(num="7102", origin="OKC", destination="DFW",
             dep=time(10, 30), arr=time(11, 40))
replace_schedule(U, [A, B])
write(A.id, always={"airborne_seen": 1, "last_on_ground": 1,
                    "off_observed": A.dep_datetime_utc().isoformat()})
cur = get_current_info(U, B.dep_datetime_utc() - timedelta(minutes=15)).current
check("airborne-then-on-the-ground is enough without a confirmed touchdown",
      cur is not None and cur.flight_number == "7102",
      str(cur and cur.flight_number))

# The negative: on the ground BEFORE ever flying is pushback, not arrival,
# and must not hand anything over.
U = make_user("sig_pushback")
A = make_leg(num="7201", origin="DFW", destination="OKC",
             dep=time(8, 0), arr=time(9, 10))
B = make_leg(num="7202", origin="OKC", destination="DFW",
             dep=time(10, 30), arr=time(11, 40))
replace_schedule(U, [A, B])
write(A.id, always={"last_on_ground": 1,
                    "out_observed": A.dep_datetime_utc().isoformat(),
                    "off_observed": A.dep_datetime_utc().isoformat()})
cur = get_current_info(U, B.dep_datetime_utc() - timedelta(minutes=15)).current
check("a leg on the ground that never got airborne does NOT hand over",
      cur is not None and cur.flight_number == "7201",
      str(cur and cur.flight_number))


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
