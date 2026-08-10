"""The v5.3 UI fixes, each against the case that exposed it.

Run: python tests_ui_fixes.py
"""
import os
import sys
import tempfile
from datetime import datetime, time as _t, timedelta, timezone
from zoneinfo import ZoneInfo

os.environ["PT_DB_FILE"] = os.path.join(tempfile.mkdtemp(), "ui_test.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import main as app_main                          # noqa: E402
from app import tags                                      # noqa: E402
from app.airports import enrich_leg                       # noqa: E402
from app.auth import create_user                          # noqa: E402
from app.db import get_connection, init_db                # noqa: E402
from app.flights import get_flight, save_schedule, write  # noqa: E402
from app.models import FlightLeg                          # noqa: E402
from app.parser import parse_schedule_text                # noqa: E402
from app.schedule import get_current_info                 # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (f"  [{detail}]" if detail and not cond else ""))


def leg(lid, date, num, o, d, dep, arr, dh=False, arr_date=None):
    l = FlightLeg(id=lid, date=date, flight_number=num, origin=o, destination=d,
                  dep_time_local=dep, arr_time_local=arr, is_deadhead=dh)
    enrich_leg(l)
    return l


# ---------------------------------------------------------------- overnight
def test_overnight():
    print("\nThe layover that straddles past/upcoming (LFT, 33h)")
    # The real lines, and the real clock: mid-morning on the 10th, so the
    # arrival is in `past` and the departure is in `upcoming`.
    legs = parse_schedule_text(
        "08/09/2026 4187 DFW 1812 LFT 1927\n"
        "08/11/2026 3779 LFT 0600 DFW 0740\n"
    )
    idx = app_main.overnight_index(legs)
    from datetime import date as _d
    entry = idx.get(_d(2026, 8, 9))
    check("the layover is found at all", entry is not None)
    if entry:
        check("duration is the full 33h33m", entry["duration"] == "33h 33m",
              entry["duration"])
        check("counted as 2 nights, not 1", entry["nights"] == 2,
              str(entry["nights"]))
        check("city is the layover city", "Lafayette" in (entry["city"] or ""),
              str(entry["city"]))

    # And it still reaches the template through the split lists.
    now = datetime(2026, 8, 10, 15, 0, tzinfo=ZoneInfo("UTC"))
    past, upcoming = [legs[0]], [legs[1]]
    nums = app_main._assign_trip_day_numbers(legs)
    groups = app_main.group_legs_by_day(past, nums, now, "24", {}, idx)
    check("the past-list group carries the overnight",
          groups and groups[0]["overnight"] is not None)
    check("the upcoming-list group does not repeat it",
          all(g["overnight"] is None
              for g in app_main.group_legs_by_day(upcoming, nums, now, "24", {}, idx)))

    print("\nAn ordinary single overnight still reads as one")
    legs2 = parse_schedule_text(
        "08/27/2026 3397 DFW 1227 BTR 1357\n"
        "08/28/2026 3925 BTR 0808 DFW 0950\n"
    )
    e2 = app_main.overnight_index(legs2).get(_d(2026, 8, 27))
    check("single overnight found", e2 is not None)
    if e2:
        check("counted as 1 night", e2["nights"] == 1, str(e2["nights"]))


# ------------------------------------------------------------- placeholders
def test_placeholder_purge():
    print("\nPlaceholder legs imported before v5.2 are cleaned out")
    conn = get_connection()
    conn.execute("INSERT OR REPLACE INTO flights (id, date, flight_number, origin, "
                 "destination) VALUES ('2026-07-05-0-DFW-DFW','2026-07-05','0','DFW','DFW')")
    conn.execute("INSERT OR REPLACE INTO flights (id, date, flight_number, origin, "
                 "destination) VALUES ('2026-07-05-3991-DCA-BNA','2026-07-05','3991','DCA','BNA')")
    conn.execute("INSERT OR REPLACE INTO roster (user_id, flight_id) "
                 "VALUES (1,'2026-07-05-0-DFW-DFW')")
    conn.commit()
    conn.close()

    init_db()   # re-runs the migration, as a container restart would

    conn = get_connection()
    ids = {r["id"] for r in conn.execute("SELECT id FROM flights")}
    roster = {r["flight_id"] for r in conn.execute("SELECT flight_id FROM roster")}
    conn.close()
    check("the DFW-DFW placeholder is gone", "2026-07-05-0-DFW-DFW" not in ids)
    check("its roster entry went with it", "2026-07-05-0-DFW-DFW" not in roster)
    check("the real leg survived", "2026-07-05-3991-DCA-BNA" in ids)


# ------------------------------------------------------------------- phase
def test_untracked_phase(uid):
    print("\nA past leg the poller never saw does not claim to be Scheduled")
    now = datetime.now(timezone.utc)
    old = leg("old-untracked", (now - timedelta(days=2)).date(), "3403",
              "DFW", "CRP", _t(10, 41), _t(12, 7))
    soon = leg("future-leg", (now + timedelta(days=2)).date(), "3403",
               "DFW", "CRP", _t(10, 41), _t(12, 7))
    save_schedule(uid, [old, soon])
    idx = app_main.tag_index(uid)

    v_old = app_main.leg_view(old, now, "24", idx)
    v_new = app_main.leg_view(soon, now, "24", idx)
    check("a two-day-old untracked leg is not 'Scheduled'",
          v_old["phase_tag"] != tags.PHASE_SCHEDULED, str(v_old["phase_tag"]))
    check("...it reads 'Not tracked'",
          v_old["phase_tag"] == app_main.PHASE_UNTRACKED, str(v_old["phase_tag"]))
    check("a genuinely future leg still reads 'Scheduled'",
          v_new["phase_tag"] == tags.PHASE_SCHEDULED, str(v_new["phase_tag"]))

    write(old.id, always={"phase_tag": tags.PHASE_ARRIVED, "closed": 1})
    idx = app_main.tag_index(uid)
    check("a real recorded phase is left alone",
          app_main.leg_view(old, now, "24", idx)["phase_tag"] == tags.PHASE_ARRIVED)


# -------------------------------------------------------------- sequencing
def test_sequencing(uid):
    print("\nFlight sequencing: late vs. landed-but-not-closed")
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    tz = ZoneInfo("America/Chicago")

    def at(offset_min):
        return (base + timedelta(minutes=offset_min)).astimezone(tz)

    # Leg A "arrived" 4 hours ago on paper; leg B is due out now.
    a_dep, a_arr = at(-330), at(-240)
    b_dep, b_arr = at(0), at(90)
    A = leg("seq-A", a_dep.date(), "3500", "DFW", "LBB",
            _t(a_dep.hour, a_dep.minute), _t(a_arr.hour, a_arr.minute))
    B = leg("seq-B", b_dep.date(), "3501", "LBB", "DFW",
            _t(b_dep.hour, b_dep.minute), _t(b_arr.hour, b_arr.minute))
    save_schedule(uid, [A, B])

    # 1. A is STILL AIRBORNE, four hours past its paper arrival.
    write(A.id, always={"airborne_seen": 1, "landed_seen": 0, "closed": 0})
    cur = get_current_info(uid, base).current
    check("a still-airborne leg keeps the card past the 3h grace",
          cur is not None and cur.id == "seq-A", str(cur and cur.id))

    # 2. It lands but gate-in never publishes, so it cannot close.
    write(A.id, always={"landed_seen": 1})
    cur = get_current_info(uid, base).current
    check("once down, it releases the card even though it never closed",
          cur is not None and cur.id == "seq-B", str(cur and cur.id))
    past_ids = [l.id for l in get_current_info(uid, base).past]
    check("...and it lands in past flights, not nowhere",
          "seq-A" in past_ids, str(past_ids))

    # 3. Premature handover: A airborne again, B's clock open but B has
    #    NOT actually gone anywhere.
    write(A.id, always={"landed_seen": 0})
    cur = get_current_info(uid, base).current
    check("B's clock opening does not steal the card from an airborne A",
          cur is not None and cur.id == "seq-A", str(cur and cur.id))

    # 4. B genuinely departs. Now it wins, immediately.
    write(B.id, always={"airborne_seen": 1})
    cur = get_current_info(uid, base).current
    check("B taking off does take the card",
          cur is not None and cur.id == "seq-B", str(cur and cur.id))

    # 5. A stuck airborne flag cannot hold the card forever.
    write(B.id, always={"airborne_seen": 0})
    far = base + timedelta(hours=14)
    cur = get_current_info(uid, far).current
    check("the 12h ceiling releases a stuck airborne flag",
          cur is None or cur.id != "seq-A", str(cur and cur.id))




# ------------------------------------------------------- the flight list
def test_flight_list(uid):
    print("\nOne list: past, the live flight, then upcoming")
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    tz = ZoneInfo("America/Chicago")

    def mk(lid, dep_off, arr_off, num):
        dl = (base + timedelta(minutes=dep_off)).astimezone(tz)
        al = (base + timedelta(minutes=arr_off)).astimezone(tz)
        l = FlightLeg(id=lid, date=dl.date(), flight_number=num,
                      origin="DFW", destination="LBB",
                      dep_time_local=_t(dl.hour, dl.minute),
                      arr_time_local=_t(al.hour, al.minute))
        enrich_leg(l)
        return l

    older = mk("L-old", -1400, -1320, "3001")     # yesterday
    recent = mk("L-recent", -600, -520, "3002")   # earlier today
    live = mk("L-live", -40, 50, "3003")          # airborne now
    nxt = mk("L-next", 300, 380, "3004")          # later today
    save_schedule(uid, [older, recent, live, nxt])
    write("L-live", always={"airborne_seen": 1})

    info = get_current_info(uid, base)
    check("the live leg is current", info.current is not None
          and info.current.id == "L-live", str(info.current and info.current.id))

    nums = app_main._assign_trip_day_numbers(info.all_legs)
    onts = app_main.overnight_index(info.all_legs)
    groups = app_main.build_flight_list(info, nums, base, "24",
                                        app_main.tag_index(uid), onts)
    rows = [r for g in groups for r in g["legs"]]
    ids = [r["id"] for r in rows]

    check("every leg appears exactly once", len(ids) == len(set(ids)) == 4, str(ids))
    check("the live flight IS in the list, not just the card",
          "L-live" in ids, str(ids))
    check("order is chronological", ids == ["L-old", "L-recent", "L-live", "L-next"],
          str(ids))

    i = ids.index("L-live")
    check("the most recent past sits immediately above the live flight",
          ids[i - 1] == "L-recent", str(ids))
    check("the next flight sits immediately below it",
          ids[i + 1] == "L-next", str(ids))
    check("the oldest is furthest up", ids[0] == "L-old", str(ids))

    by_id = {r["id"]: r for r in rows}
    check("past rows are flagged past",
          by_id["L-old"]["is_past"] and by_id["L-recent"]["is_past"])
    check("the live row is flagged current and NOT past",
          by_id["L-live"]["is_current"] and not by_id["L-live"]["is_past"])
    check("the upcoming row is neither",
          not by_id["L-next"]["is_past"] and not by_id["L-next"]["is_current"])

    print("\nWhich days collapse when past flights are hidden")
    yesterday = [g for g in groups if all(r["id"] == "L-old" for r in g["legs"])]
    check("a wholly-past day is marked all_past",
          yesterday and yesterday[0]["all_past"] is True)
    mixed = [g for g in groups if any(r["id"] == "L-live" for r in g["legs"])]
    check("a day holding the live flight is NOT all_past",
          mixed and mixed[0]["all_past"] is False)
    check("...but its flown leg is still individually hidden",
          mixed and any(r["is_past"] for r in mixed[0]["legs"]))
    check("exactly one scroll landmark",
          sum(1 for g in groups if g["first_live"]) == 1)
    landmark = [g for g in groups if g["first_live"]][0]
    check("the landmark is the first non-past day",
          landmark["all_past"] is False)


def test_past_detail_available(uid2):
    print("\nA past flight still hands over its gate and baggage")
    base = datetime.now(timezone.utc)
    tz = ZoneInfo("America/Chicago")
    dl = (base - timedelta(hours=30)).astimezone(tz)
    al = (base - timedelta(hours=28)).astimezone(tz)
    l = FlightLeg(id="P-detail", date=dl.date(), flight_number="4187",
                  origin="DFW", destination="LFT",
                  dep_time_local=_t(dl.hour, dl.minute),
                  arr_time_local=_t(al.hour, al.minute))
    enrich_leg(l)
    save_schedule(uid2, [l])
    write("P-detail", always={
        "phase_tag": "Arrived", "closed": 1, "closed_by": "airline",
        "arrival_source": "airline", "gate_destination": "2",
        "baggage_claim": "1", "tail_api": "N204NN",
        "aircraft_type": "Embraer 175"})

    from app.view import build as view_build
    from app.flights import get_flight
    payload = view_build(get_flight("P-detail"), l, base, "24")
    check("the arrival gate survives into the past",
          (payload.get("gates") or {}).get("dest_gate") == "2",
          str(payload.get("gates")))
    check("so does the baggage belt",
          (payload.get("gates") or {}).get("baggage") == "1")
    check("so does the aircraft",
          (payload.get("aircraft") or {}).get("registration") == "N204NN")
    check("and how it closed out", payload.get("closed_by") == "airline")


def main():
    init_db()
    uid = create_user("uitest", "pw-not-used")
    test_overnight()
    test_placeholder_purge()
    test_untracked_phase(uid)
    test_sequencing(uid)
    test_flight_list(create_user("listtest", "pw-not-used"))
    test_past_detail_available(create_user("detailtest", "pw-not-used"))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
