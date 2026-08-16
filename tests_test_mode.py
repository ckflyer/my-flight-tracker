"""Test mode, and who is allowed to run it. (1.6.0)

Two features, one suite, because they are the same claim from two sides:
this install can be OPERATED — rehearsed, inspected, handed to a second
administrator — without SSH, without SQL, and without money.

THE THING THIS SUITE MOSTLY GUARDS is that a rehearsal cannot touch
anything real. Test mode invents flights, and an invention that leaks is
worse than no test mode at all: a simulated leg that spent an AeroAPI
credit costs money for a flight that does not exist, and one that reached
a logbook would put a fiction in a legal record. So the isolation tests
outnumber the "does it work" tests on purpose.

The other half is that the simulator must NOT fake the app's answers. It
produces position reports and nothing else; `flightmatch`, `tags` and
`closure` then run on them unchanged. A simulator that wrote `closed = 1`
directly would prove nothing at all, so several tests here assert that the
closure REASON is the real production route.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["PT_DB_FILE"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["TRACK_POLLER_ENABLED"] = "0"

from app.db import init_db, get_connection                # noqa: E402
from app import enrichment, poller, simulator             # noqa: E402
from app.auth import (count_admins, create_user, list_all_users,  # noqa: E402
                      set_admin)
from app.flights import (flight_key, get_flight,          # noqa: E402
                         load_schedule, rows_awaiting_gate_in, write)
from app.schedule import get_current_info                 # noqa: E402

init_db()
UTC = timezone.utc
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"  {detail}" if detail and not cond else ""))


def fly(uid, leg, start, sweeps, step=20):
    """Run the real poller over a simulated leg. Returns the final row."""
    t = start
    for _ in range(sweeps):
        t += timedelta(seconds=step)
        poller.poll_once(t)
    return get_flight(leg.id), t


UID = create_user("owner", "password123")
T0 = datetime.now(UTC)


# ---------------------------------------------------------------------------
print("\n-- a rehearsal cannot touch anything real --")

legs = simulator.start(UID, "normal", T0)
LEG = legs[0]
row = get_flight(LEG.id)
check("the leg is flagged as simulated", bool(row["simulated"]))
check("...and records which scenario made it", row["sim_scenario"] == "normal")
check("...using a flight number no regional operates",
      int(row["flight_number"]) >= simulator.SIM_FLIGHT_BASE,
      row["flight_number"])

# The money guard. Give the account a key and a budget, then prove that
# every paid path refuses on the grounds of simulation alone.
conn = get_connection()
conn.execute("UPDATE users SET aeroapi_enabled = 1, aeroapi_key = 'k', "
             "aeroapi_budget = 50.0 WHERE id = ?", (UID,))
conn.commit()
conn.close()

CALLS = {"n": 0}


def exploding_fetch(*a, **k):
    CALLS["n"] += 1
    raise AssertionError("a simulated leg asked AeroAPI for real data")


enrichment.fetch_leg = exploding_fetch
enrichment.refresh_usage = lambda uid, now: False

check("refresh() refuses a simulated leg outright",
      enrichment.refresh(UID, LEG, T0, has_adsb=True, departed=True) is None)
check("...before spending anything", CALLS["n"] == 0)
check("backfill_gate_in() refuses one too",
      enrichment.backfill_gate_in(UID, LEG, T0 + timedelta(days=1)) is None)
check("...also before spending anything", CALLS["n"] == 0)

# The guard must not depend on the leg being open, closed, or anything else
# that a scenario could change.
write(LEG.id, always={"closed": 1, "closed_by": "backstop",
                      "closed_at": T0.isoformat()})
check("a CLOSED simulated leg is never chased for a late gate-in",
      not any(r["id"] == flight_key(LEG.id)
              for r in rows_awaiting_gate_in(T0 + timedelta(days=1))))
check("...and asking directly still refuses",
      enrichment.backfill_gate_in(UID, LEG, T0 + timedelta(days=1)) is None)
check("no AeroAPI call was made at any point", CALLS["n"] == 0)

# The ADS-B guard. The network provider must never be reached either — not
# for cost, but because the shared rate limiter is a finite resource the
# real legs are relying on.
ADSB = {"n": 0}


def exploding_live_state(callsign):
    ADSB["n"] += 1
    raise AssertionError("a simulated leg asked the ADS-B provider")


poller.live_state = exploding_live_state
simulator.stop(UID)
LEG = simulator.start(UID, "normal", T0)[0]
row, t = fly(UID, LEG, T0, 90)
check("the ADS-B provider is never asked about a simulated leg", ADSB["n"] == 0)
check("...and no AeroAPI call happened during a whole flight", CALLS["n"] == 0)
check("...with the leg's own ticket counter untouched",
      int(row["api_queries_used"]) == 0, str(row["api_queries_used"]))


# ---------------------------------------------------------------------------
print("\n-- but the app's own logic runs on it, unchanged --")

check("the leg actually flew", bool(row["airborne_seen"]) and bool(row["landed_seen"]))
check("...reached Arrived", row["phase_tag"] == "Arrived", str(row["phase_tag"]))
check("...and closed on a REAL production route, not a simulator write",
      row["closed_by"] in ("observed", "airline", "relaunch", "backstop"),
      str(row["closed_by"]))
check("...specifically the observed route for a clean leg",
      row["closed_by"] == "observed", str(row["closed_by"]))
check("a track was recorded, as for any leg",
      get_flight(LEG.id) is not None)


# ---------------------------------------------------------------------------
print("\n-- each scenario reproduces the bug it is named for --")

# The 1.4.0 taxi-in trap: parked, still transmitting, no silence anywhere.
simulator.stop(UID)
TRAP = simulator.start(UID, "taxi_in_trap", T0)[0]
row, t = fly(UID, TRAP, T0, 75)
check("taxi-in trap: still open while the APU runs", not row["closed"],
      str(row["closed_by"]))
check("...and correctly sitting in Taxi-in", row["phase_tag"] == "Taxi-in",
      str(row["phase_tag"]))
simulator.age(TRAP.id, 30)
poller.poll_once(t + timedelta(seconds=20))
row = get_flight(TRAP.id)
check("...closing on the LONG STOP once aged 30 minutes",
      bool(row["closed"]) and row["closed_by"] == "observed",
      str(row["closed_by"]))

# Coverage lost in cruise: never seen to land, so only the backstop can end it.
simulator.stop(UID)
DARK = simulator.start(UID, "coverage_loss", T0)[0]
row, t = fly(UID, DARK, T0, 80)
check("coverage loss: never seen to land", not row["landed_seen"])
check("...so no observed route can fire", not row["closed"])
simulator.age(DARK.id, 200)
poller.poll_once(t + timedelta(seconds=20))
row = get_flight(DARK.id)
check("...and the BACKSTOP is what ends it",
      bool(row["closed"]) and row["closed_by"] == "backstop",
      str(row["closed_by"]))

# The has_departed guard. Nothing may close a leg that never flew.
simulator.stop(UID)
NEVER = simulator.start(UID, "never_departs", T0)[0]
row, t = fly(UID, NEVER, T0, 80)
simulator.age(NEVER.id, 600)
poller.poll_once(t + timedelta(seconds=20))
check("a leg that never departs still cannot close, at any age",
      not get_flight(NEVER.id)["closed"])

# The 1.5.0 handover, which is the whole reason this scenario exists.
simulator.stop(UID)
TURN = simulator.start(UID, "turn", T0)
check("the turn scenario builds two legs", len(TURN) == 2, str(len(TURN)))
check("...that are an out and back",
      TURN[0].origin == TURN[1].destination and
      TURN[0].destination == TURN[1].origin)
t, timeline = T0, []
for _ in range(160):
    t += timedelta(seconds=20)
    poller.poll_once(t)
    cur = get_current_info(UID, t).current
    label = cur.flight_number if cur else None
    if not timeline or timeline[-1] != label:
        timeline.append(label)
check("the card starts on leg 1", timeline and timeline[0] == TURN[0].flight_number,
      str(timeline))
check("...and hands over to leg 2", TURN[1].flight_number in timeline, str(timeline))
check("...exactly once, with no flapping between them", len(timeline) == 2,
      str(timeline))


# ---------------------------------------------------------------------------
print("\n-- ageing shifts what happened, never what was scheduled --")

simulator.stop(UID)
AGE = simulator.start(UID, "taxi_in_trap", T0)[0]
row, t = fly(UID, AGE, T0, 75)
before = (row["date"], row["dep_time_local"], row["arr_time_local"])
stopped_before = row["stopped_since"]
simulator.age(AGE.id, 45)
row = get_flight(AGE.id)
check("the scheduled date and times are NOT moved",
      (row["date"], row["dep_time_local"], row["arr_time_local"]) == before)
check("...but the observed stop is", row["stopped_since"] != stopped_before)
shift = (datetime.fromisoformat(stopped_before)
         - datetime.fromisoformat(row["stopped_since"])).total_seconds()
check("...by exactly the requested amount", abs(shift - 45 * 60) < 1, str(shift))
check("ageing a leg that is not simulated is refused",
      simulator.age("2026-01-01-1234-DFW-OKC", 30) is False)


# ---------------------------------------------------------------------------
print("\n-- stopping leaves nothing behind --")

n = simulator.stop(UID)
check("stop reports what it removed", n >= 1, str(n))
check("no simulated flight rows survive", simulator.active_sim_rows() == [])
conn = get_connection()
orphan_roster = conn.execute(
    "SELECT COUNT(*) AS n FROM roster WHERE flight_id NOT IN "
    "(SELECT id FROM flights)").fetchone()["n"]
orphan_pos = conn.execute(
    "SELECT COUNT(*) AS n FROM positions WHERE flight_key NOT IN "
    "(SELECT id FROM flights)").fetchone()["n"]
conn.close()
check("...no orphaned roster rows", orphan_roster == 0, str(orphan_roster))
check("...no orphaned position tracks", orphan_pos == 0, str(orphan_pos))
check("...and the roster is empty again", load_schedule(UID) == [])
check("stopping twice is harmless", simulator.stop(UID) == 0)
check("starting a scenario clears any previous one",
      len(simulator.start(UID, "normal", T0)) == 1 and
      len(simulator.active_sim_rows()) == 1)
simulator.stop(UID)
check("an unknown scenario key creates nothing",
      simulator.start(UID, "not-a-scenario", T0) == [])


# ---------------------------------------------------------------------------
print("\n-- creating a second admin --")

DAVE = create_user("fo_dave", "password123")
check("only the FIRST account is an admin automatically", count_admins() == 1)
check("...and it is the owner",
      [u["username"] for u in list_all_users() if u["is_admin"]] == ["owner"])

check("promoting works", set_admin(DAVE, True) is True)
check("...and there are now two", count_admins() == 2)
check("promoting an existing admin changes nothing", set_admin(DAVE, True) is False)
check("demoting works", set_admin(DAVE, False) is True)
check("...back to one", count_admins() == 1)

check("THE LAST ADMIN CANNOT BE REMOVED", set_admin(UID, False) is False)
check("...so an install can never end up with data and no administrator",
      count_admins() == 1)
check("promoting an account that does not exist is refused",
      set_admin(99999, True) is False)


# ---------------------------------------------------------------------------
print("\n-- everything that runs the install is on one page --")

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "templates", "admin.html"), encoding="utf-8") as fh:
    ah = fh.read()
with open(os.path.join(here, "templates", "settings.html"), encoding="utf-8") as fh:
    sh = fh.read()
with open(os.path.join(here, "app", "main.py"), encoding="utf-8") as fh:
    src = fh.read()

check("the people table is on /admin", "people-table" in ah)
check("...with a Make admin control", "/admin/users/promote/" in ah)
check("...and it is gone from Settings", "people-table" not in sh)
check("Settings points at /admin instead", 'href="/admin"' in sh)
check("test mode is on /admin", "/admin/test/start" in ah)
check("diagnostics and the decision log are linked from /admin",
      "/admin/diagnostics" in ah and "/admin/debug" in ah)
check("the old Settings delete route still redirects rather than 404ing",
      'return RedirectResponse(url="/admin#people", status_code=307)' in src)
check("every admin route checks is_admin",
      src.count('if not pilot["is_admin"]:') >= 7,
      str(src.count('if not pilot["is_admin"]:')))
check("an admin cannot demote themselves",
      'if user_id == pilot["id"]:' in src)
check("admin panels render only for admins", "{% if is_admin %}" in ah)
check("ageing is bounded and only ever backwards",
      "max(1, min(int(minutes), 720))" in src)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
