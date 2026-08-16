"""Regression cover for the past-leg blank-card bug (v4.5).

compute_live_payload used to return early for any leg that wasn't the live
one, so a flight's stored actual times, gates and closeout record became
invisible the moment it aged out of the 3-hour grace window. This pins the
fixed behaviour on both non-live paths:

  * a PAST leg still reports its enrichment and frozen closeout
  * a not-yet-departed leg (T-30 preview) reports gate and delay
  * neither reports a live countdown, which would be nonsense

Run: python tests_past_leg_detail.py   (from the flight-tracker directory)
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["PT_DB_FILE"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db import init_db, get_connection          # noqa: E402
from app.auth import create_user                     # noqa: E402
from app.models import FlightLeg                     # noqa: E402
from app.airports import enrich_leg                  # noqa: E402
from app import closure                              # noqa: E402
from app.flights import get_flight, replace_schedule, write  # noqa: E402
from app.main import compute_live_payload            # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


def make_leg(leg_id, date, dep, arr):
    from datetime import time as _t
    leg = FlightLeg(id=leg_id, date=date, flight_number="3880",
                    origin="DFW", destination="GRK",
                    dep_time_local=_t(*map(int, dep.split(":"))),
                    arr_time_local=_t(*map(int, arr.split(":"))))
    enrich_leg(leg)
    return leg


LEGS = []


def insert_leg(uid, leg, sort_index):
    """Legs live in `flights` now — one row carrying everything."""
    LEGS.append(leg)
    replace_schedule(uid, LEGS)


def seed_airline(user_id, leg_id, fields, fetched_at):
    """Stand in for what a real AeroAPI query would have written.

    Straight into the columns, because that is where enrichment puts it
    now — there is no JSON blob to seed any more.
    """
    fields = dict(fields)
    fields["last_api_query_at"] = fetched_at.isoformat()
    fields["api_queries_used"] = 1
    write(leg_id, always=fields)


def main():
    init_db()
    uid = create_user("testpilot", "pw12345678")

    # --- A flown, closed-out leg from yesterday -----------------------
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    past = make_leg("past-1", yesterday, "17:57", "18:52")
    insert_leg(uid, past, 0)

    dep_utc = past.dep_datetime_utc()
    arr_utc = past.arr_datetime_utc()
    seed_airline(uid, past.id, {
        "out_actual_api": (dep_utc + timedelta(minutes=2)).isoformat(),
        "in_actual_api": (arr_utc - timedelta(minutes=6)).isoformat(),
        "gate_origin": "B34", "terminal_origin": "B",
        "gate_destination": "A1",
    }, datetime.now(timezone.utc) - timedelta(hours=20))
    closure.close(past, get_flight(past.id), closure.SOURCE_AIRLINE,
                  arr_utc, None)

    now = datetime.now(timezone.utc)
    live, extra = compute_live_payload(uid, past, False, now, 15, "12")

    print("\npast leg (closed out yesterday):")
    check("reports the arrival time", bool(extra.get("arr_delay")),
          repr(extra.get("arr_delay")))
    check("arrival reads 6 min early",
          (extra.get("arr_delay") or {}).get("minutes") == -6,
          repr((extra.get("arr_delay") or {}).get("minutes")))
    check("departure tinted late at zero tolerance",
          (extra.get("dep_delay") or {}).get("state") == "late",
          repr((extra.get("dep_delay") or {}).get("state")))
    check("origin gate survives", (extra.get("gates") or {}).get("origin_gate") == "B34")
    check("dest gate survives", (extra.get("gates") or {}).get("dest_gate") == "A1")
    check("closeout is reported", extra.get("closed") is True)
    check("closed_by preserved", extra.get("closed_by") == "airline")
    check("arrival_source preserved", extra.get("arrival_source") == "airline")
    check("phase pill reads Arrived", extra.get("phase_tag") == "Arrived",
          repr(extra.get("phase_tag")))
    check("no status pill on a normal completed flight",
          extra.get("status_tag") is None, repr(extra.get("status_tag")))
    check("no live countdown on a past leg", extra.get("ete") is None,
          repr(extra.get("ete")))
    check("no progress bar on a past leg", extra.get("progress_pct") is None)
    check("no live position", live is None)

    # --- A leg departing in 25 minutes (T-30 preview already ran) -----
    soon_dep = datetime.now(timezone.utc) + timedelta(minutes=25)
    soon = make_leg("soon-1", soon_dep.date(),
                    soon_dep.strftime("%H:%M"),
                    (soon_dep + timedelta(minutes=55)).strftime("%H:%M"))
    insert_leg(uid, soon, 1)

    sdep = soon.dep_datetime_utc()
    seed_airline(uid, soon.id, {
        "out_scheduled": sdep.isoformat(),
        "out_estimated": (sdep + timedelta(minutes=14)).isoformat(),
        "gate_origin": "C12",
    }, datetime.now(timezone.utc) - timedelta(minutes=3))

    live, extra = compute_live_payload(uid, soon, False, datetime.now(timezone.utc), 15, "12")
    print("\nupcoming leg (T-25, preview enrichment stored):")
    check("gate visible before departure",
          (extra.get("gates") or {}).get("origin_gate") == "C12")
    check("published delay visible before departure",
          (extra.get("dep_delay") or {}).get("state") == "late",
          repr((extra.get("dep_delay") or {}).get("state")))
    check("enrichment freshness reported", bool(extra.get("enriched_at")),
          repr(extra.get("enriched_at")))
    check("not closed", not extra.get("closed"))

    # The airline pushed departure 14 min past the FFDO time, so this one
    # SHOULD carry the pill — unlike a flight that merely left late.
    from app import tags
    row = get_flight(soon.id)
    status, dep_rev, _ = tags.compute_status(row, soon, datetime.now(timezone.utc))
    check("a real airline push lights the Delayed pill",
          status == tags.STATUS_DELAYED, repr(status))
    check("the push is measured against the FFDO time", dep_rev == 14, repr(dep_rev))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
