"""Does the pilot's monthly spend limit actually stop AeroAPI queries?

The setting is worthless if it only *displays* a cap. These tests go at the
real enforcement point — refresh(), the single function the poller
calls to spend money — and assert that no HTTP call is made once the limit
is reached. fetch_leg is replaced with a spy that raises if invoked, so a
regression that lets a query through fails loudly rather than silently
costing the pilot money.

Run: python tests_budget_limit.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

os.environ["PT_DB_FILE"] = os.path.join(tempfile.mkdtemp(), "budget_test.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db import init_db, get_connection             # noqa: E402
from app.auth import create_user                       # noqa: E402
from app import enrichment                             # noqa: E402
from app.settings import AppSettings, load_settings, save_settings  # noqa: E402
from app.models import FlightLeg                       # noqa: E402
from app.airports import enrich_leg                    # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))


def set_spend(user_id, queries):
    """Set the local query tally for the current accounting period."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET aeroapi_period = ?, aeroapi_queries = ?, "
            "aeroapi_reported_cost = NULL, aeroapi_usage_at = NULL WHERE id = ?",
            (datetime.now(timezone.utc).strftime("%Y-%m"), queries, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_reported(user_id, cost, age_hours=0.0):
    when = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET aeroapi_reported_cost = ?, aeroapi_reported_calls = ?, "
            "aeroapi_usage_at = ? WHERE id = ?",
            (cost, 100, when.isoformat(), user_id),
        )
        conn.commit()
    finally:
        conn.close()


def a_leg():
    """A leg 20 minutes past its scheduled departure — inside the window
    where should_query() genuinely wants to spend a query, so that a
    blocked call proves the BUDGET stopped it and not the schedule."""
    from datetime import time as _t
    # dep_time_local is LOCAL TO THE ORIGIN, so it has to be built in
    # Phoenix time. Building it from UTC clock hands puts the leg seven
    # hours out and quietly moves it outside the query window, which makes
    # a blocked call prove nothing.
    dep_local = datetime.now(ZoneInfo("America/Phoenix")) - timedelta(minutes=20)
    arr_local = dep_local.astimezone(ZoneInfo("America/Chicago")) + timedelta(hours=2)
    leg = FlightLeg(id="budget-leg-1", date=dep_local.date(), flight_number="3232",
                    origin="PHX", destination="DFW",
                    dep_time_local=_t(dep_local.hour, dep_local.minute),
                    arr_time_local=_t(arr_local.hour, arr_local.minute))
    enrich_leg(leg)
    return leg


class Spy:
    """Stands in for fetch_leg. Records calls; never touches the network."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return ({"ident": "ENY3232", "cancelled": False, "diverted": False}, {}, 1)


def main():
    init_db()
    uid = create_user("budgettest", "pw-not-used-here")
    save_settings(uid, AppSettings(aeroapi_enabled=True, aeroapi_key="fake-key-for-test",
                                   aeroapi_budget=4.50))

    cpq = enrichment.COST_PER_QUERY_USD
    now = datetime.now(ZoneInfo("UTC"))
    leg = a_leg()

    print("\nBudget setting round-trips")
    save_settings(uid, AppSettings(aeroapi_enabled=True, aeroapi_key="fake-key-for-test",
                                   aeroapi_budget=12.75))
    check("saved limit reads back", load_settings(uid).aeroapi_budget == 12.75,
          str(load_settings(uid).aeroapi_budget))
    check("budget_state reflects the pilot's number",
          enrichment.budget_state(uid)["budget"] == 12.75)
    check("allow_overage key is gone", "allow_overage" not in enrichment.budget_state(uid))

    print("\nCap arithmetic")
    save_settings(uid, AppSettings(aeroapi_enabled=True, aeroapi_key="fake-key-for-test",
                                   aeroapi_budget=1.00))
    set_spend(uid, int(0.50 / cpq))          # $0.50 of $1.00
    st = enrichment.budget_state(uid)
    check("under limit is not exhausted", st["exhausted"] is False, f"spent={st['spent']}")

    set_spend(uid, int(1.00 / cpq))          # exactly $1.00
    st = enrichment.budget_state(uid)
    check("at limit is exhausted", st["exhausted"] is True, f"spent={st['spent']}")

    set_spend(uid, int(3.00 / cpq))          # way over
    check("over limit is exhausted", enrichment.budget_state(uid)["exhausted"] is True)

    print("\nEnforcement: refresh must not spend once capped")
    spy = Spy()
    real_fetch = enrichment.fetch_leg
    enrichment.fetch_leg = spy
    try:
        set_spend(uid, int(3.00 / cpq))      # over the $1.00 limit
        enrichment.refresh(uid, leg, now, has_adsb=False)
        check("no API call while over limit", spy.calls == 0, f"calls={spy.calls}")

        # Raising the limit must let queries resume — a cap that latches
        # permanently would be just as broken as one that never fires.
        save_settings(uid, AppSettings(aeroapi_enabled=True, aeroapi_key="fake-key-for-test",
                                       aeroapi_budget=50.00))
        before = spy.calls
        enrichment.refresh(uid, leg, now, has_adsb=False)
        check("raising the limit resumes queries", spy.calls > before,
              f"calls went {before} -> {spy.calls}")

        # Zero means off, even with a key saved and budget untouched.
        save_settings(uid, AppSettings(aeroapi_enabled=True, aeroapi_key="fake-key-for-test",
                                       aeroapi_budget=0.0))
        set_spend(uid, 0)
        before = spy.calls
        enrichment.refresh(uid, leg, now, has_adsb=False)
        check("limit of 0 blocks all queries", spy.calls == before, f"calls={spy.calls}")

        # FlightAware's own reported figure should cap us too, not just our
        # local estimate — it's the number that actually gets billed.
        save_settings(uid, AppSettings(aeroapi_enabled=True, aeroapi_key="fake-key-for-test",
                                       aeroapi_budget=2.00))
        set_spend(uid, 0)
        set_reported(uid, 9.99, age_hours=0.0)
        st = enrichment.budget_state(uid)
        check("reported spend is the source when fresh", st["source"] == "reported")
        before = spy.calls
        enrichment.refresh(uid, leg, now, has_adsb=False)
        check("reported spend over limit blocks queries", spy.calls == before,
              f"calls={spy.calls}")

        # A stale reported figure must not keep us capped forever.
        set_reported(uid, 9.99, age_hours=48.0)
        st = enrichment.budget_state(uid)
        check("stale reported figure falls back to estimate", st["source"] == "estimated")
    finally:
        enrichment.fetch_leg = real_fetch

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED: " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
