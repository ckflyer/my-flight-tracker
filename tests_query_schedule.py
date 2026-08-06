"""Regression cover for the per-trigger query caps (v4.6).

The caps in should_query() are enforced by counters in refresh(), and those
counters key off the trigger's REASON STRING. Through v4.5 the delay
counter tested for "T+15" while the trigger returned "T+20: departure
check", so _delay_tries never advanced, the `tries == 0` branch stayed
true, and a flight stuck at the gate re-asked the same question every
MIN_QUERY_GAP until MAX_QUERIES_PER_LEG stopped it — eight queries where
three were intended.

This walks the clock forward over a flight that never leaves the gate and
asserts the trigger sequence, which is what would have caught it.

Run: python tests_query_schedule.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone, time as dtime

os.environ["PT_DB_FILE"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db import init_db                    # noqa: E402
from app.models import FlightLeg              # noqa: E402
from app.airports import enrich_leg           # noqa: E402
from app import enrichment as e               # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


def build_leg(dep):
    leg = FlightLeg(id="stuck", date=dep.date(), flight_number="3880",
                    origin="DFW", destination="GRK",
                    dep_time_local=dtime(0, 0), arr_time_local=dtime(0, 0))
    enrich_leg(leg)
    leg.__class__.dep_datetime_utc = lambda self, _d=dep: _d
    leg.__class__.arr_datetime_utc = lambda self, _d=dep + timedelta(hours=1): _d
    return leg


def walk(leg, dep, steps=12):
    """Replay should_query + refresh's counter updates without spending."""
    enr, used, fired = None, 0, []
    now = dep + timedelta(minutes=21)
    for _ in range(steps):
        reason = e.should_query(enr, leg, now, used, down=False, touchdown=None,
                                has_adsb=True, departed=False, took_off_at=None)
        if reason:
            fired.append(reason)
            used += 1
            prev = enr or {}
            enr = {"_fetched_at": now.isoformat(),
                   "scheduled_out": dep.isoformat(), "_queries": used}
            # Mirrors refresh(). If these prefixes drift from the strings
            # should_query returns, the caps stop working.
            enr["_delay_tries"] = int(prev.get("_delay_tries", 0)) + (
                1 if (reason.startswith("T+20")
                      or reason.startswith("still on the ground")) else 0)
        now += timedelta(minutes=21)
    return fired


def main():
    init_db()
    dep = datetime.now(timezone.utc) - timedelta(minutes=30)
    fired = walk(build_leg(dep), dep)

    print("\nflight that never leaves the gate:")
    for r in fired:
        print("    ->", r)
    print()

    check("takes exactly one first look",
          sum(1 for r in fired if r.startswith("T-30")) == 1)
    check("asks the T+20 departure question exactly once",
          sum(1 for r in fired if r.startswith("T+20")) == 1,
          f"got {sum(1 for r in fired if r.startswith('T+20'))}")
    check("ground watch respects MAX_DELAY_WATCH_TRIES",
          sum(1 for r in fired if r.startswith("still on the ground"))
          == e.MAX_DELAY_WATCH_TRIES - 1)
    check("whole ground-delay path stays within 4 queries",
          len(fired) <= 4, f"spent {len(fired)}")
    check("never reaches the per-leg ceiling on a gate hold",
          len(fired) < e.MAX_QUERIES_PER_LEG)

    # The prefixes refresh() tests for must exist in should_query's output.
    check("no trigger reason starts with the stale 'T+15'",
          not any(r.startswith("T+15") for r in fired))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
