"""The v5.3 UI fixes, each against the case that exposed it.

Run: python tests_ui_fixes.py
"""
import os
import re
import sys
import tempfile
from datetime import date, datetime, time as _t, timedelta, timezone
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
    # Pinned to midday LOCAL, not the real clock. "L-old" sits 1400 minutes
    # back, which only lands on yesterday if the suite runs before about
    # 23:20 — after that it folds into today, the wholly-past day group
    # vanishes and the suite fails. It failed exactly once, at 23:34, which
    # is how this was spotted. Anchoring to noon keeps every offset inside
    # the day it was written for, whatever time the suite is run.
    tz = ZoneInfo("America/Chicago")
    base = (datetime.now(tz).replace(hour=12, minute=0, second=0, microsecond=0)
            .astimezone(timezone.utc))

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


# ----------------------------------------------------------- time lines
def test_time_lines():
    print("\nTwo rows carrying time AND variance, not three")
    from app.view import _time_line, _variance
    base = datetime(2026, 8, 11, 17, 34, tzinfo=timezone.utc)

    late = _variance(base, (base + timedelta(minutes=12)).isoformat(), None, None,
                     "America/Chicago", "24", "Departing", "Departed")
    line = _time_line(late, base, "America/Chicago", "24")
    check("a late departure shows the revised time",
          line["time"] == "12:46 CDT", str(line))
    check("...the scheduled one it moved from", line["was"] == "12:34 CDT", str(line))
    check("...and by how much", line["note"] == "12 min late", str(line))
    check("...tagged so it can be tinted", line["state"] == "late", str(line))

    early = _variance(base, (base - timedelta(minutes=7)).isoformat(), None, None,
                      "America/Chicago", "24", "Arriving", "Arrived")
    eline = _time_line(early, base, "America/Chicago", "24")
    check("an early arrival reads early", eline["note"] == "7 min early", str(eline))

    ontime = _variance(base, base.isoformat(), None, None,
                       "America/Chicago", "24", "Arriving", "Arrived")
    oline = _time_line(ontime, base, "America/Chicago", "24")
    check("on time says so", oline["note"] == "on time", str(oline))
    check("...and does not strike through an identical time",
          oline["was"] is None, str(oline))

    # The case the old "Scheduled" row used to cover.
    unflown = _time_line(None, base, "America/Chicago", "24")
    check("an unflown leg still shows its scheduled time",
          unflown and unflown["time"] == "12:34 CDT", str(unflown))
    check("...with no variance clutter",
          unflown["note"] is None and unflown["was"] is None, str(unflown))
    check("no baseline at all yields nothing to draw",
          _time_line(None, None, "America/Chicago", "24") is None)


# ------------------------------------------------------------ template audit
def test_template_contract():
    """Grep-level guards on viewer.html.

    Not a substitute for looking at the page, but this template has twice
    lost working JavaScript to colliding edits (see NOTES in the README),
    and every check below is something that failed silently rather than
    loudly when it broke: the page still rendered, it was just wrong.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()

    # v5.6: the route strip is on the ALWAYS-VISIBLE part of the card.
    strip_at = html.find('<div class="route-strip">')
    details_at = html.find('id="expand-details"')
    check("route strip exists", strip_at != -1)
    check("...and sits above the collapsible detail",
          strip_at != -1 and details_at != -1 and strip_at < details_at)
    for el in ("progress-fill-el", "route-plane-el", "progress-label-el"):
        check(f"{el} present for the poller to write to", f'id="{el}"' in html)

    # v5.6: the flight list is no longer behind the card's disclosure.
    check("expand-wrap is not display:none",
          ".expand-wrap { display: none; }" not in html)
    check("...and setExpanded no longer toggles it",
          "wrap.classList.toggle" not in html)

    # v5.6 bug: applyEnrichment was nested inside the progress branch, so
    # gates and revised times only repainted when a live fix existed.
    poll = html[html.find("function refreshLiveData"):]
    poll = poll[:poll.find("function selectLeg")]
    for call in ("applyPills(", "applyProgress(", "applyEnrichment("):
        idx = poll.find(call)
        check(f"{call.rstrip('(')} is called each poll", idx != -1)
        if idx != -1:
            line_start = poll.rfind("\n", 0, idx) + 1
            indent = len(poll[line_start:idx]) - len(poll[line_start:idx].lstrip())
            check(f"...at the top level of the poll handler ({call.rstrip('(')})",
                  indent <= 12, f"indent={indent}")

    # Still true from v4.5/v5.5 — these are the two that went missing before.
    check("togglePast is defined, not just called",
          html.count("function togglePast") == 1)
    check("tickRelativeTimes survives", "function tickRelativeTimes" in html)

    # Display-time rounding: track.py keeps one decimal, the card must not
    # show it now that the figure is permanently on screen.
    check("percentage is rounded for display",
          "current.progress_pct|round|int" in html and "Math.round(pct)" in html)
    check("distance is rounded for display",
          "current.distance_nm|round|int" in html)


def test_today_is_a_local_day():
    """"Today" must be resolved in the local zone, not UTC.

    An instant is fine in UTC. A CALENDAR DAY is not: `now.date()` on a
    UTC clock rolls over at 7pm Central, so all evening the calendar
    highlighted tomorrow and the agenda anchor pointed at the wrong day.
    Reported from a screenshot timestamped 23:19 local.
    """
    utc = ZoneInfo("UTC")
    central = ZoneInfo("America/Chicago")
    evening = datetime(2026, 8, 13, 4, 19, tzinfo=utc)   # 23:19 on the 12th
    check("a UTC date() is wrong late in the evening",
          evening.date() == date(2026, 8, 13))
    check("...and converting to local first is right",
          evening.astimezone(central).date() == date(2026, 8, 12))

    # Midday is the case that hid this for so long: both agree.
    midday = datetime(2026, 8, 12, 17, 0, tzinfo=utc)    # 12:00 Central
    check("both agree at midday, which is why it went unnoticed",
          midday.date() == midday.astimezone(central).date() == date(2026, 8, 12))

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "app", "main.py"), encoding="utf-8") as fh:
        src = fh.read()
    cal = src[src.find("async def calendar_page"):]
    cal = cal[:cal.find("template = jinja_env.get_template")]
    check("the calendar route converts before taking a date",
          "now.astimezone().date()" in cal)
    check("...and no bare now.date() survives there",
          "= now.date()" not in cal)


def test_one_palette_everywhere():
    """No template may carry its own copy of the colour variables."""
    here = os.path.dirname(os.path.abspath(__file__))
    tdir = os.path.join(here, "templates")
    names = sorted(n for n in os.listdir(tdir) if n.endswith(".html"))
    check("all eleven templates collapsed to ten", len(names) == 10, str(len(names)))

    for name in names:
        with open(os.path.join(tdir, name), encoding="utf-8") as fh:
            html = fh.read()
        check(f"{name} declares no palette of its own", "--bg:" not in html)
        check(f"{name} links the shared stylesheet", "/static/app.css" in html)

    with open(os.path.join(here, "static", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    # The five logged-out pages have no data-theme attribute to read and no
    # account to read a preference from, so they follow the OS. The :not()
    # guard stops that overriding a pilot who explicitly chose dark.
    check("logged-out pages follow the system theme",
          "prefers-color-scheme: light" in css)
    check("...without overriding an explicit choice",
          ":root:not([data-theme])" in css)
    for var in ("--bg", "--card", "--text", "--muted", "--border", "--input-bg"):
        check(f"{var} is defined for dark and light",
              css.count(var + ":") >= 3, str(css.count(var + ":")))


def test_settings_is_one_page():
    """Viewers and pilots share a template; role decides what renders."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "settings.html"), encoding="utf-8") as fh:
        html = fh.read()
    check("the API key section is pilot-only",
          html.find("{% if is_pilot %}") < html.find("Airline flight data"))
    check("the admin roster needs BOTH flags", "{% if is_pilot and is_admin %}" in html)
    check("the form action is supplied by the route", 'action="{{ post_to }}"' in html)
    check("account recovery is gated", "{% if is_pilot %}\n  <div class=\"card\">\n    <h2>Account recovery" in html)
    check("the old viewer template is gone",
          not os.path.exists(os.path.join(here, "templates", "viewer_settings.html")))

    # Both forms post the same field names now — they used to disagree
    # (show_flightaware vs show_fa), which is what two templates cost.
    with open(os.path.join(here, "app", "main.py"), encoding="utf-8") as fh:
        src = fh.read()
    vp = src[src.find("async def viewer_settings_post"):]
    vp = vp[:vp.find("return resp")]
    check("the viewer route accepts the pilot field name",
          "show_flightaware" in vp)
    check("...while the stored cookie name is unchanged",
          "pt_viewer_show_fa" in vp)


def test_zone_never_wraps_a_time():
    """A zone is its own element, and is stated once when it can be."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    check("list rows print the bare time, not time+zone",
          '<span class="row-chip-time">{{ leg.dep_short or leg.dep }}</span>' in html)
    # The label rides beside the time it belongs to. It used to sit at the
    # far right of the row, pushed there by margin-left:auto, stranded in
    # empty space away from the number it described.
    check("the zone is no longer flung to the row's edge", "row-tz-single" not in html)
    check("same zone both ends states it once, on the arrival",
          '{% if leg.arr_zone %}<span class="tz">{{ leg.arr_zone }}</span>{% endif %}' in html)
    check("a crossing states it at the departure too",
          "not leg.same_zone and leg.dep_zone" in html)
    check("the card no longer prints an 'All times' line", "All times" not in html)
    check("...and the style that positioned it is gone", ".route-tz" not in html)

    # The tap-to-reveal bubble is gone: undiscoverable, and it made the
    # card and the list state zones by two different rules.
    for dead in ("time-pop", "data-full", "data-pop"):
        check(f"no trace of the old {dead} bubble", dead not in html)


def test_zone_rule_reaches_every_page():
    """The Flights table and import review follow the same zone rule."""
    here = os.path.dirname(os.path.abspath(__file__))

    with open(os.path.join(here, "app", "main.py"), encoding="utf-8") as fh:
        src = fh.read()
    check("the Flights table asks for bare times",
          src.count('fmt_local(leg, "dep", settings.time_format, with_zone=False)') == 1)
    check("...and carries the zone separately",
          '"dep_zone": tz_abbr(leg, "dep")' in src)
    # Scoped to the Flights route: leg_view's "date" is a machine-readable
    # ISO string used for grouping and anchors, and should stay that way.
    admin_rows = src[src.find('"date_iso"') - 900:src.find('"date_iso"') + 400]
    check("the Flights table prints a date a person would say",
          'leg.date.strftime("%b %d")' in admin_rows)
    check("...keeping the ISO one only for the delete confirmation",
          '"date_iso": str(leg.date)' in admin_rows)

    with open(os.path.join(here, "templates", "admin.html"), encoding="utf-8") as fh:
        html = fh.read()
    check("zone gets its own column", 'class="zone-cell"' in html)
    check("...and the divider spans all seven", 'colspan="7"' in html)
    check("the leg counter pluralises properly", "leg(s)" not in html)
    check("...with real logic behind it",
          "{{ '' if count == 1 else 's' }}" in html)
    check("delete still confirms against an unambiguous date",
          "row.date_iso" in html)


def test_nothing_render_blocking_is_remote():
    """No page may pull a script or stylesheet from someone else's server."""
    here = os.path.dirname(os.path.abspath(__file__))
    tdir = os.path.join(here, "templates")
    for name in sorted(n for n in os.listdir(tdir) if n.endswith(".html")):
        with open(os.path.join(tdir, name), encoding="utf-8") as fh:
            html = fh.read()
        remote = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
        check(f"{name} loads nothing offsite", not remote, str(remote))

    for rel in ("static/vendor/leaflet/leaflet.js",
                "static/vendor/leaflet/leaflet.css",
                "static/vendor/leaflet/images/marker-icon.png",
                "static/vendor/Sortable.min.js"):
        check(f"{rel} is vendored", os.path.exists(os.path.join(here, rel)))

    # Leaflet's stylesheet points at its images relatively, so they have to
    # sit beside it or markers silently vanish.
    with open(os.path.join(here, "static/vendor/leaflet/leaflet.css"), encoding="utf-8") as fh:
        css = fh.read()
    check("leaflet css uses relative image paths", "images/marker-icon.png" in css)


def test_bottom_tab_bar():
    here = os.path.dirname(os.path.abspath(__file__))
    tdir = os.path.join(here, "templates")
    for name in ("viewer.html", "calendar.html", "admin.html", "settings.html"):
        with open(os.path.join(tdir, name), encoding="utf-8") as fh:
            html = fh.read()
        check(f"{name} has a tab bar", '<nav class="tabbar"' in html)
        check(f"{name} dropped the old top nav", "topnav" not in html)
        check(f"{name} marks exactly one tab active", html.count('class="active"') == 1,
              str(html.count('class="active"')))

    with open(os.path.join(here, "static", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    check("the bar clears the home indicator", "env(safe-area-inset-bottom)" in css)
    check("pages reserve room so it covers nothing",
          "padding-bottom: calc(72px" in css)
    check("the pill is offset to the left, not full width",
          "border-radius: 999px" in css and "display: inline-flex" in css)

    # Viewers have no Flights page to reach.
    with open(os.path.join(tdir, "viewer.html"), encoding="utf-8") as fh:
        v = fh.read()
    i = v.find('<nav class="tabbar"')
    check("Flights is pilot-only in the bar", "{% if is_pilot %}" in v[i:i + 2000])


def test_full_bleed_map():
    """The map is a fixed backdrop; the page scrolls over it."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    bg = html[html.find(".map-bg {"):]
    bg = bg[:bg.find("}")]
    check("the map covers the whole viewport", "position: fixed" in bg and "inset: 0" in bg)
    check("...behind everything else", "z-index: 0" in bg)
    # Leaflet's panes climb to z-index 800 and would otherwise paint over
    # the card and the tab bar sitting above them.
    check("leaflet stacking is confined", "isolation: isolate" in bg)
    check("the negative-margin fake bleed is gone", ".map-wrap" not in html)

    # The gradient panel over the topbar was covering the map buttons
    # beneath it. A halo on the text is legible and covers nothing.
    check("no scrim panel over the controls", ".topbar::before" not in html)
    check("the brand gets a text halo instead", "text-shadow" in html)

    check("there is a scrim that fades the map back", ".scroll-scrim" in html)
    check("...and a spacer letting it show above the card", ".hero-space" in html)
    check("controls are positioned against the hero strip", "--hero" in html)


def test_scroll_reveal():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    check("the schedule starts hidden behind the map", 'id="reveal"' in html)
    rev = html[html.find(".reveal {"):]
    rev = rev[:rev.find("}")]
    # NOT hidden in CSS. v6.1 shipped .reveal { opacity: 0 } and relied on
    # the script to bring it back, so when Leaflet 404'd and the script died
    # the schedule vanished along with the map. The script opts INTO the
    # effect once it is known to be running.
    check("the schedule is not hidden by CSS alone", "opacity: 0" not in rev)
    check("...the script hides it only once alive",
          "reveal.style.opacity = '0';" in html)
    # A one-leg day would otherwise leave the page too short to scroll, so
    # the list could never be revealed at all.
    check("...with the page guaranteed scrollable", "min-height" in rev)
    check("scroll drives it, not a timed animation", "requestAnimationFrame(paint)" in html)
    check("invisible rows cannot be tapped", "pointerEvents" in html)
    check("reduced motion skips the effect", "prefers-reduced-motion" in html)

    # The failure that produced v6.2: the reveal lived INSIDE the map's
    # IIFE, so a missing Leaflet took the map and the schedule down together.
    scroll_at = html.find("// ---- Scroll reveal")
    map_at = html.find("const mapEl = document.getElementById('flight-map')")
    check("the reveal does not live inside the map block",
          scroll_at != -1 and map_at != -1 and scroll_at < map_at)
    check("a missing Leaflet is caught, not thrown",
          "typeof L === 'undefined'" in html)
    check("...and says so instead of showing a blank", "Map unavailable" in html)
    check("...and hands the schedule back", "_ptRevealOff" in html)


def test_show_on_map_action():
    """Tapping a row still expands in place; the map move is explicit."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    check("each row offers Show on map", "data-show-on-map" in html)
    check("...wired to selectLeg", "selectLeg(onMap.getAttribute('data-show-on-map'))" in html)
    check("...and returns you to the map", "window.scrollTo({ top: 0" in html)
    check("a bare row tap still only expands",
          "It deliberately\n      // does NOT move the card or the map" in html
          or "does NOT move the card" in html)


def test_settings_budget_saves():
    """The default budget must satisfy the field's own validation."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "settings.html"), encoding="utf-8") as fh:
        html = fh.read()
    check("the spend limit steps in cents", 'id="aeroapi-budget" step="0.01"' in html)
    check("...not quarters", 'step="0.25"' not in html)

    from app.settings import AppSettings
    default = AppSettings().aeroapi_budget
    cents = round(default * 100)
    check("the default budget is a whole number of cents",
          abs(default * 100 - cents) < 1e-9, str(default))
    # This is the bug: 4.90 is not a multiple of 0.25, so the browser
    # rejected the value the app itself had put in the box.
    check("...and would have failed a 0.25 step", cents % 25 != 0, str(default))


def test_two_letter_zones():
    from app.main import tz_abbr, _TWO_LETTER_ZONE
    from app.models import FlightLeg
    from app.airports import enrich_leg
    from datetime import date as _d, time as _tm

    def leg(o, d, on):
        l = FlightLeg(id="z", date=on, flight_number="1", origin=o, destination=d,
                      dep_time_local=_tm(7, 0), arr_time_local=_tm(9, 0))
        enrich_leg(l)
        return l

    winter = leg("DFW", "PHX", _d(2026, 12, 15))
    summer = leg("DFW", "PHX", _d(2026, 7, 15))
    check("central reads CT in December", tz_abbr(winter, "dep") == "CT",
          str(tz_abbr(winter, "dep")))
    check("...and CT in July too", tz_abbr(summer, "dep") == "CT")
    check("mountain reads MT", tz_abbr(winter, "arr") == "MT")
    check("eastern reads ET", tz_abbr(leg("DFW", "JFK", _d(2026, 1, 5)), "arr") == "ET")
    check("hawaii reads HT", tz_abbr(leg("LAX", "HNL", _d(2026, 1, 5)), "arr") == "HT")
    # The whole point: a label that never claims daylight or standard time
    # cannot be wrong about which one is in force, which retires the
    # fixed-July-sample bug rather than fixing it.
    for v in _TWO_LETTER_ZONE.values():
        check(f"{v} states no daylight/standard", "S" not in v[:-1] and "D" not in v[:-1])


def test_no_hardcoded_palette_colours():
    """A hex from the dark palette must not survive in any template.

    The five logged-out pages hardcoded background: #0f1419 on their text
    inputs. That was invisible while those pages were dark no matter what;
    once they started following the system theme, a light-mode user got a
    white page with black input boxes.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    tdir = os.path.join(here, "templates")
    dark = ("#0f1419", "#1a2332", "#e7ecf3", "#8b9bb4", "#2a3548")
    for name in sorted(n for n in os.listdir(tdir) if n.endswith(".html")):
        with open(os.path.join(tdir, name), encoding="utf-8") as fh:
            html = fh.read()
        for hexcode in dark:
            check(f"{name} does not hardcode {hexcode}", hexcode not in html)


def test_map_remeasures():
    """Leaflet must re-measure once layout has actually happened.

    The map is sized by a fixed, full-viewport parent and the script runs
    mid-layout. Mobile Safari reports that box as 0x0 at that point, so
    Leaflet cached zero dimensions and requested no tiles at all — blank on
    phones, fine on desktop.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    check("the map re-measures itself", "invalidateSize" in html)
    check("...after layout, not during it", "requestAnimationFrame(remeasure)" in html)
    check("...and on rotation", "orientationchange" in html)
    check("...and whenever the box changes size", "ResizeObserver" in html)


def test_html_is_never_cached():
    """Assets are versioned and cacheable; pages are not and must not be."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "app", "main.py"), encoding="utf-8") as fh:
        src = fh.read()
    mw = src[src.find("async def _no_stale_html"):]
    mw = mw[:mw.find("app.add_middleware")]
    check("html responses are marked uncacheable", "no-store" in mw)
    check("...scoped to html only", 'ctype.startswith("text/html")' in mw)
    check("...leaving versioned assets alone", "static" not in mw.split('"""')[-1])


def main():
    init_db()
    uid = create_user("uitest", "pw-not-used")
    test_template_contract()
    test_today_is_a_local_day()
    test_one_palette_everywhere()
    test_settings_is_one_page()
    test_zone_never_wraps_a_time()
    test_zone_rule_reaches_every_page()
    test_nothing_render_blocking_is_remote()
    test_bottom_tab_bar()
    test_full_bleed_map()
    test_two_letter_zones()
    test_scroll_reveal()
    test_show_on_map_action()
    test_settings_budget_saves()
    test_no_hardcoded_palette_colours()
    test_map_remeasures()
    test_html_is_never_cached()
    test_overnight()
    test_placeholder_purge()
    test_untracked_phase(uid)
    test_sequencing(uid)
    test_flight_list(create_user("listtest", "pw-not-used"))
    test_past_detail_available(create_user("detailtest", "pw-not-used"))
    test_time_lines()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
