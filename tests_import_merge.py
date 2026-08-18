"""N1: an import ADDS, and cannot revise history. (1.5.0)

THE BUG. `save_schedule` replaced a pilot's roster: any leg not in the new
paste had its roster row deleted. Pasting September therefore erased
August from that pilot's view. Combined with the old 30-day retention this
made the app a rolling window, which is exactly what a logbook cannot be.

Note what was NOT broken and must stay that way: flight ROWS are shared
and adopted, never duplicated. Only the roster LINK was pruned. Every test
here that checks a deletion also checks the flight row survived, because
the fix must not swing the other way and start deleting real data.

Three rules, and each one is a different failure it prevents:

  SCOPE IS THE MONTH  — a September paste says nothing about August, so
                        reconciliation is confined to the months pasted.
  ONLY THE FUTURE     — a leg that already departed happened; an import
                        must not be able to revise it.
  NOTHING IS SILENT   — removals are proposed, not performed. The confirm
                        step only honours what the review page offered.
"""
import os
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone

os.environ["PT_DB_FILE"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["TRACK_POLLER_ENABLED"] = "0"

from app.db import init_db, get_connection              # noqa: E402
from app.airports import enrich_leg                     # noqa: E402
from app.flights import (flight_key, get_flight, load_schedule,   # noqa: E402
                         merge_schedule, remove_legs, replace_schedule, write)
from app.importer import (ADDED, CHANGED, REMOVED, UNCHANGED,     # noqa: E402
                          build_diff, month_labels, months_covered)
from app.models import FlightLeg                        # noqa: E402
from app.parser import parse_schedule_text              # noqa: E402

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


def leg(on, num, origin="DFW", destination="OKC",
        dep=time(14, 0), arr=time(15, 10), dh=False):
    l = FlightLeg(id=f"{on.isoformat()}-{num}-{origin}-{destination}", date=on,
                  flight_number=num, origin=origin, destination=destination,
                  dep_time_local=dep, arr_time_local=arr, is_deadhead=dh)
    enrich_leg(l)
    return l


UID = make_user()
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
AUG_PAST = date(2026, 8, 4)        # flown
AUG_SOON = date(2026, 8, 28)       # upcoming, same month
SEP = date(2026, 9, 12)            # next month


# ---------------------------------------------------------------------------
print("\n-- the bug: a new month must not erase the old one --")

august = [leg(AUG_PAST, "3729"), leg(AUG_PAST, "3566", "OKC", "DFW",
                                     time(19, 11), time(20, 11)),
          leg(AUG_SOON, "3900", "DFW", "TUL")]
merge_schedule(UID, august)
check("August imported", len(load_schedule(UID)) == 3)

september = [leg(SEP, "4100", "DFW", "ICT"), leg(SEP, "4101", "ICT", "DFW",
                                                 time(18, 0), time(19, 10))]
merge_schedule(UID, september)
ids = {l.id for l in load_schedule(UID)}
check("THE BUG: pasting September keeps August", len(ids) == 5, str(len(ids)))
check("...every August leg is still there",
      all(flight_key(l.id) in ids for l in august))
check("...and September was added",
      all(flight_key(l.id) in ids for l in september))


# ---------------------------------------------------------------------------
print("\n-- the roster stays in chronological order --")

sched = load_schedule(UID)
dates = [l.date for l in sched]
check("legs come back in date order", dates == sorted(dates), str(dates))
# Import them the other way round; order must not depend on paste order.
merge_schedule(UID, list(reversed(september)) + list(reversed(august)))
dates = [(l.date, l.dep_time_local) for l in load_schedule(UID)]
check("...regardless of the order they were pasted in",
      dates == sorted(dates), str(dates))
check("...and re-importing the same legs creates no duplicates",
      len(load_schedule(UID)) == 5, str(len(load_schedule(UID))))


# ---------------------------------------------------------------------------
print("\n-- the diff: scope is the month, and only the future --")

# A September paste that DROPS 4101 and adds 4102.
paste = [leg(SEP, "4100", "DFW", "ICT"), leg(SEP, "4102", "ICT", "DFW",
                                             time(20, 0), time(21, 10))]
d = build_diff(paste, load_schedule(UID), NOW)
removed_ids = {e["leg"].id for e in d[REMOVED]}
added_ids = {e["leg"].id for e in d[ADDED]}

check("the dropped September leg is proposed for removal",
      flight_key(september[1].id) in {flight_key(i) for i in removed_ids})
check("the new September leg is proposed as an addition",
      flight_key(paste[1].id) in {flight_key(i) for i in added_ids})
check("SCOPE: no August leg is proposed for removal",
      not any(e["leg"].date.month == 8 for e in d[REMOVED]),
      str([str(e["leg"].date) for e in d[REMOVED]]))
check("the unchanged September leg is reported as unchanged",
      len(d[UNCHANGED]) == 1 and d[UNCHANGED][0]["leg"].flight_number == "4100")

# Now an AUGUST paste that omits both flown legs and the upcoming one.
aug_paste = [leg(AUG_SOON, "3901", "DFW", "SGF")]
d2 = build_diff(aug_paste, load_schedule(UID), NOW)
rem = {e["leg"].flight_number for e in d2[REMOVED]}
# THE IMPORT HAS THE FINAL SAY, BUT NEVER SILENTLY (owner's call, 1.20.0,
# replacing "future only"). A flown leg the paste omits IS offered now —
# the old rule left no way to say "that trip came off my line and someone
# else flew it". What keeps it safe is the `flown` flag: the review page
# ticks upcoming removals and leaves flown ones unticked, because pasting
# a single trip routinely says nothing about the rest of the month.
check("the upcoming August leg is proposed for removal",
      "3900" in rem, str(rem))
check("...and so are the two already-flown ones",
      "3729" in rem and "3566" in rem, str(rem))
by_num = {e["leg"].flight_number: e for e in d2[REMOVED]}
check("...but the flown ones are FLAGGED as flown",
      by_num["3729"]["flown"] and by_num["3566"]["flown"])
check("...and the upcoming one is not",
      not by_num["3900"]["flown"])

# A FLOWN LEG IS NEVER MODIFIED (owner's call, 1.20.0; made absolute in
# 1.22.0). Not its times, not its deadhead flag, not its trip break. The
# FFDO time is the SCHEDULE and a flown flight's schedule is set in stone;
# what actually happened lives in the OOOI columns. Re-pasting a month
# used to report every flown leg as "changed" because the airline's record
# had settled to what occurred — noise on every re-import, describing an
# edit the confirm step did not even make.
#
# 1.20.0 kept ONE exception, for the deadhead flag, on the strength of a
# logbook that is no longer being built. The exception is gone: the diff
# has to agree with what merge will do, and merge freezes flown legs
# outright, so listing one as "changed" would promise an edit the confirm
# step declines to make.
flown_retimed = [leg(AUG_PAST, "3729", "DFW", "OKC", dep="09:31", arr="10:44")]
flown_retimed[0].is_deadhead = not flown_retimed[0].is_deadhead
d3 = build_diff(flown_retimed, load_schedule(UID), NOW)
check("a flown leg with different pasted times is NOT reported as changed",
      not any(e["leg"].flight_number == "3729" for e in d3[CHANGED]),
      str([e["leg"].flight_number for e in d3[CHANGED]]))
check("...it is reported as unchanged",
      any(e["leg"].flight_number == "3729" for e in d3[UNCHANGED]),
      str([e["leg"].flight_number for e in d3[UNCHANGED]]))
check("...and a flipped deadhead flag does not change that either",
      not any(e["leg"].flight_number == "3729" for e in d3[CHANGED]),
      str([e["leg"].flight_number for e in d3[CHANGED]]))


# ---------------------------------------------------------------------------
print("\n-- changed times are shown, not silently applied --")

moved = leg(SEP, "4100", "DFW", "ICT", time(15, 30), time(16, 40))
d3 = build_diff([moved], load_schedule(UID), NOW)
check("a retimed leg is reported as changed", len(d3[CHANGED]) == 1)
check("...carrying the OLD times so the pilot can see what moved",
      d3[CHANGED][0]["was"].dep_time_local == time(14, 0),
      str(d3[CHANGED][0]["was"].dep_time_local))
check("...and is not double-counted as an addition", len(d3[ADDED]) == 0)

dh_flip = leg(SEP, "4100", "DFW", "ICT", dh=True)
d4 = build_diff([dh_flip], load_schedule(UID), NOW)
check("a leg becoming a deadhead is a change", len(d4[CHANGED]) == 1)

# A route change is a different flight, so it is one removal and one add
# rather than a silent edit — the id itself encodes the route.
rerouted = leg(SEP, "4100", "DFW", "TUL")
d5 = build_diff([rerouted], load_schedule(UID), NOW)
check("a rerouted leg is an add plus a remove, never a silent edit",
      len(d5[ADDED]) == 1 and any(e["leg"].destination == "ICT"
                                  for e in d5[REMOVED]))


# ---------------------------------------------------------------------------
print("\n-- applying only what was approved --")

before = len(load_schedule(UID))
remove_legs(UID, [flight_key(september[1].id)])
after = load_schedule(UID)
check("an approved removal takes the roster row", len(after) == before - 1)
check("...but the SHARED FLIGHT ROW survives",
      get_flight(september[1].id) is not None)

# The flight row keeps everything it learned, so re-adding restores it.
write(september[1].id, always={"gate_destination": "C31", "closed": 1,
                               "closed_by": "airline"})
merge_schedule(UID, [september[1]])
check("re-adding a removed leg adopts the existing row, blanks nothing",
      get_flight(september[1].id)["gate_destination"] == "C31")
check("...including its closeout record",
      get_flight(september[1].id)["closed_by"] == "airline")

check("removing nothing is a no-op", remove_legs(UID, []) == 0)
check("removing a leg that is not on the roster removes nothing",
      remove_legs(UID, ["2026-01-01-9999-AAA-BBB"]) == 0)


# ---------------------------------------------------------------------------
print("\n-- a manual add covers what no bid line ever will --")

# The case: a diversion that then continued to the original destination.
diversion = leg(AUG_SOON, "3900", "TUL", "OKC", time(17, 0), time(17, 55))
merge_schedule(UID, [diversion])
ids = {l.id for l in load_schedule(UID)}
check("a hand-added leg joins the roster",
      flight_key(diversion.id) in ids)
check("...without disturbing the leg it continued from",
      flight_key(leg(AUG_SOON, "3900", "DFW", "TUL").id) in ids)


# ---------------------------------------------------------------------------
print("\n-- month scoping details --")

check("months_covered reads the paste, not the clock",
      months_covered(september) == {"2026-09"})
check("a paste spanning a month boundary claims both months",
      months_covered(august + september) == {"2026-08", "2026-09"})
check("one month reads as a month", month_labels({"2026-09"}) == "September 2026")
check("two months read as a range",
      month_labels({"2026-08", "2026-09"}) == "August–September 2026")
check("a year boundary keeps both years",
      month_labels({"2026-12", "2027-01"}) == "December 2026–January 2027")
check("an empty paste claims nothing", month_labels(set()) == "")

# An empty paste must not be read as "delete everything".
d6 = build_diff([], load_schedule(UID), NOW)
check("an empty paste proposes no removals at all", d6[REMOVED] == [])


# ---------------------------------------------------------------------------
print("\n-- the destructive primitive is no longer on the import path --")

import app.main as main                                  # noqa: E402
src = open(os.path.join(os.path.dirname(__file__), "app", "main.py")).read()
check("main.py never calls replace_schedule",
      "replace_schedule" not in src)
check("the import confirm route merges", "merge_schedule(pilot" in src)
check("...and removes only what the page offered",
      'form.getlist("removable_id")' in src)
# 1.7.0: the hand-add route is GONE. It was inferred from N1's spec line
# about a diversion that continued on, not asked for; inventing UI from an
# inference is how a page fills up with things nobody wanted.
check("there is no hand-add route", '"/admin/add"' not in src)

check("parse still drops FFDO placeholder lines",
      parse_schedule_text("07/05/2026 0 DFW 1946 DFW 1946") == [])


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
