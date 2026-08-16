"""Can a deadhead still run away with the month's budget?

The bug this guards against: before v5.2 a FAILED carrier lookup wrote
nothing down, so the poller — which sweeps every 20 seconds — asked the
identical question on the next sweep, and the next. A deadhead sits in the
poller's window for five or six hours, which is roughly a thousand billed
/schedules queries on ONE leg, against a budget check this path never
consulted and a counter it never incremented.

These tests call resolve() the way the poller does: over and over, with
the lookup failing every time. The paid attempts must stop at two.

Run: python tests_carrier_cap.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["PT_DB_FILE"] = os.path.join(tempfile.mkdtemp(), "carrier_test.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import time as _t                          # noqa: E402

from app import carrier                                  # noqa: E402
from app.airports import enrich_leg                      # noqa: E402
from app.auth import create_user                         # noqa: E402
from app.db import init_db                               # noqa: E402
from app.flights import get_flight, replace_schedule        # noqa: E402
from app.models import FlightLeg                         # noqa: E402
from app.parser import parse_schedule_text               # noqa: E402
from app.settings import AppSettings, save_settings      # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (f"  [{detail}]" if detail and not cond else ""))


def a_deadhead(uid, leg_id="dh-1"):
    """A deadhead leg departing in 30 minutes, saved to the DB."""
    dep_local = datetime.now(timezone.utc) + timedelta(minutes=30)
    leg = FlightLeg(id=leg_id, date=dep_local.date(), flight_number="3232",
                    origin="PHX", destination="DFW",
                    dep_time_local=_t(dep_local.hour, dep_local.minute),
                    arr_time_local=_t((dep_local.hour + 2) % 24, dep_local.minute),
                    is_deadhead=True)
    enrich_leg(leg)
    replace_schedule(uid, [leg])
    return leg


class Counter:
    """Stands in for the paid /schedules lookup. Always fails, like the
    real thing does on a route it has no answer for."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        return None


def main():
    init_db()
    uid = create_user("carriertest", "pw-not-used")
    save_settings(uid, AppSettings(aeroapi_enabled=True, aeroapi_key="fake-key",
                                   aeroapi_budget=4.90))

    # The free ADS-B probe must never touch the network in these tests.
    carrier.resolve_via_adsb = lambda leg, now=None: None

    print("\nThe runaway loop is capped")
    leg = a_deadhead(uid)
    spy = Counter()
    real = carrier.resolve_via_aeroapi
    carrier.resolve_via_aeroapi = spy
    try:
        # 900 sweeps == five hours of the poller at 20-second intervals,
        # which is what a deadhead actually gets. Pre-v5.2 this was 900
        # billed queries.
        now = datetime.now(timezone.utc)
        for i in range(900):
            carrier.resolve(leg, "fake-key", now + timedelta(seconds=20 * i),
                            user_id=uid)
        check("900 poller sweeps cost at most 2 paid lookups",
              spy.calls <= carrier.MAX_PAID_TRIES, f"{spy.calls} calls")
        check("it did try at least once", spy.calls >= 1, f"{spy.calls} calls")

        row = get_flight(leg.id)
        check("the attempts are recorded on the row",
              int(row["carrier_tries"] or 0) == spy.calls,
              f"row={row['carrier_tries']} spy={spy.calls}")

        print("\nAttempts are spaced, not burned back to back")
        leg2 = a_deadhead(uid, "dh-2")
        spy2 = Counter()
        carrier.resolve_via_aeroapi = spy2
        base = datetime.now(timezone.utc)
        for i in range(30):        # ten minutes of sweeps
            carrier.resolve(leg2, "fake-key", base + timedelta(seconds=20 * i),
                            user_id=uid)
        check("second attempt waits out the retry gap", spy2.calls == 1,
              f"{spy2.calls} calls in 10 min")
        carrier.resolve(leg2, "fake-key", base + carrier.PAID_RETRY_GAP +
                        timedelta(minutes=1), user_id=uid)
        check("...and does fire once the gap has passed", spy2.calls == 2,
              f"{spy2.calls} calls")

        print("\nA success ends it permanently")
        leg3 = a_deadhead(uid, "dh-3")
        hits = Counter()

        def succeed(*a, **k):
            hits(*a, **k)
            return "AAL3232"

        carrier.resolve_via_aeroapi = succeed
        base = datetime.now(timezone.utc)
        for i in range(100):
            carrier.resolve(leg3, "fake-key", base + timedelta(seconds=20 * i),
                            user_id=uid)
        check("resolved once, never asked again", hits.calls == 1,
              f"{hits.calls} calls")
        check("the callsign is stored on the leg",
              leg3.operator_callsign == "AAL3232", str(leg3.operator_callsign))
        check("needs_resolution goes false", carrier.needs_resolution(leg3) is False)

        print("\nNo key means no spending, and no crash")
        leg4 = a_deadhead(uid, "dh-4")
        nokey = Counter()
        carrier.resolve_via_aeroapi = nokey
        check("without a key nothing is paid for",
              carrier.resolve(leg4, None, datetime.now(timezone.utc)) is None
              and nokey.calls == 0, f"{nokey.calls} calls")
    finally:
        carrier.resolve_via_aeroapi = real

    print("\nThe FFDO placeholder lines are dropped")
    legs = parse_schedule_text(
        "07/05/2026 3991 DCA 1351 BNA 1432\n"
        "07/05/2026 0 DFW 1946 DFW 1946\n"
        "07/21/2026 0 CMI 1751 CMI 1751\n"
        "08/04/2026 (D) 3232 PHX 1715 DFW 2150\n"
    )
    check("only the two real legs survive", len(legs) == 2, f"{len(legs)} legs")
    check("no leg has the same airport both ends",
          all(l.origin != l.destination for l in legs))
    check("the real deadhead is kept", any(l.is_deadhead for l in legs))
    check("the ordinary leg is kept", any(l.flight_number == "3991" for l in legs))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
