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
from app.flights import get_flight, replace_schedule, write  # noqa: E402
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
    replace_schedule(uid, [old, soon])
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
    replace_schedule(uid, [A, B])

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
    replace_schedule(uid, [older, recent, live, nxt])
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
    replace_schedule(uid2, [l])
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
          line["time"] == "12:46 CT", str(line))
    check("...the scheduled one it moved from", line["was"] == "12:34 CT", str(line))
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
          unflown and unflown["time"] == "12:34 CT", str(unflown))
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
    # togglePast was removed in 1.11.0 with the button that called it. The
    # assertion that replaced it is the one that matters now: nothing may
    # hide a flight behind a control, because the list is scoped instead.
    # Comments recording the removal must not read as the removal not
    # having happened — same trap as the dead-CSS checks above.
    code = re.sub(r"^\s*//.*$", "", html, flags=re.M)
    code = re.sub(r"\{#.*?#\}", "", code, flags=re.S)
    check("no Show-past-flights control survives",
          "togglePast" not in code and "past-toggle" not in code)
    check("...and nothing hides a flown leg with display:none",
          "body:not(.past-open)" not in html)
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
    # The point of this assertion was that viewer_settings.html got folded
    # into settings.html -- NOT that the template count is frozen. Adding a
    # page is allowed; carrying a private palette is not. Naming the file
    # that must stay gone says what was actually meant.
    check("viewer_settings.html stayed merged into settings.html",
          "viewer_settings.html" not in names, str(names))

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
    # The MARKUP moved to the shared strip in 1.11.0; the RULE did not.
    # Matched on shape rather than on the old literal, because a test that
    # pins one surface's markup makes replacing that surface look like a
    # regression — which is exactly what happened here.
    check("list rows print the bare time, not a glued time+zone",
          "leg.dep_line.time_short" in html and "leg.arr_line.time_short" in html)
    check("...falling back to the bare scheduled time, never the glued one",
          "or leg.dep_short or leg.dep }}" in html)
    # The label rides beside the time it belongs to. It used to sit at the
    # far right of the row, pushed there by margin-left:auto, stranded in
    # empty space away from the number it described.
    check("the zone is no longer flung to the row's edge", "row-tz-single" not in html)
    # v1.2.0 CHANGED THIS RULE DELIBERATELY. It used to be "state the zone
    # once where you can": the arrival always carried its label, the
    # departure only when the two differed, and the current-flight card
    # showed neither when they matched. Three rules visible on one screen,
    # which read as randomness rather than economy -- reported by the owner
    # as "some after both times, some after just the second time".
    #
    # Now every time carries its own zone, everywhere. Longer, and
    # predictable, which is worth more to a family member who does not know
    # that a missing label is supposed to mean "same as the other one".
    check("the arrival states its zone, as its own element",
          '{% if leg.arr_line and leg.arr_line.zone %}<span class="tz" aria-hidden="true">{{ leg.arr_line.zone }}</span>{% endif %}' in html)
    check("the departure states its zone too, unconditionally",
          '{% if leg.dep_line and leg.dep_line.zone %}<span class="tz" aria-hidden="true">{{ leg.dep_line.zone }}</span>{% endif %}' in html)
    check("no surface suppresses a zone by comparing the two",
          "not leg.same_zone" not in html and "not current.same_zone" not in html)
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

    with open(os.path.join(here, "templates", "flights.html"), encoding="utf-8") as fh:
        html = fh.read()
    # THE ZONE RIDES ON ITS OWN TIME (1.24.2, replacing a column).
    #
    # It used to be a seventh column printing "CT" or "CT/ET" once per
    # row. Every other surface in this app treats a zone as an annotation
    # ON a time and renders it as the .tz subscript — the strips, the
    # tracker card and the calendar have all done so since 1.7.0. The
    # roster was the last place stating it as a value of its own, which
    # is why it looked like a different app's table.
    check("the zone column is gone", 'class="zone-cell"' not in html)
    check("...and the divider spans the six that remain", 'colspan="6"' in html)
    check("...with the zone now subscripted onto the departure",
          '{{ row.dep }}{% if row.dep_zone %}<span class="tz"' in html)
    check("...and onto the arrival",
          '{{ row.arr }}{% if row.arr_zone %}<span class="tz"' in html)
    # The route still needs both zones available, so the view must keep
    # supplying them separately — a single combined string would make the
    # per-time subscript impossible.
    check("both zones are still supplied separately",
          '"arr_zone": tz_abbr(leg, "arr")' in src)
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
    """One tab bar, in one file. (1.7.0)

    It used to be copy-pasted into four templates. That is how /admin came
    to be labelled "Flights" in every copy while its URL said otherwise,
    and how the Tracker glyph drifted into being a different aeroplane from
    the app icon. The active item is now driven by `active_tab` from the
    route rather than by hand-editing one copy.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    tdir = os.path.join(here, "templates")
    for name in ("viewer.html", "calendar.html", "flights.html",
                 "admin.html", "settings.html"):
        with open(os.path.join(tdir, name), encoding="utf-8") as fh:
            html = fh.read()
        check(f"{name} includes the shared tab bar",
              'partials/tabbar.html' in html)
        check(f"{name} has no tab bar of its own",
              '<nav class="tabbar"' not in html)
        check(f"{name} dropped the old top nav", "topnav" not in html)

    with open(os.path.join(tdir, "partials", "tabbar.html"), encoding="utf-8") as fh:
        bar = fh.read()
    check("the bar itself has exactly one nav", bar.count('<nav class="tabbar"') == 1)
    check("...with four destinations",
          all(f'href="{h}"' in bar for h in ("/", "/calendar", "/flights", "/settings")))
    check("...pointing at /flights, not the old /admin",
          'href="/admin"' not in bar)
    check("active comes from the route, not from editing a copy",
          bar.count("active_tab ==") == 4)
    check("Flights is pilot-only in the bar", "{% if is_pilot %}" in bar)
    check("the Tracker glyph is the shared plane, not a fourth aeroplane",
          'partials/plane_glyph.html' in bar)

    # Every page that renders the bar must say which tab it is on, or the
    # bar silently shows nothing selected. The admin page is the deliberate
    # exception: it is not one of the four.
    with open(os.path.join(here, "app", "main.py"), encoding="utf-8") as fh:
        src = fh.read()
    for tab in ("tracker", "calendar", "flights", "settings"):
        check(f"the route for {tab} sets active_tab", f'"{tab}"' in src)
    check("the admin page deliberately has no active tab",
          "active_tab=None" in src)

    with open(os.path.join(here, "static", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    check("the bar clears the home indicator", "env(safe-area-inset-bottom)" in css)
    check("pages reserve room so it covers nothing",
          "padding-bottom: calc(72px" in css)
    check("the pill is offset to the left, not full width",
          "border-radius: 999px" in css and "display: inline-flex" in css)


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
    # Was "--hero" (a fixed fraction of the screen). The card is now measured
    # and parked just above the tab bar, and the variable that everything
    # else lines up against is where the card actually landed.
    check("controls are positioned against the card", "--card-top" in html)
    check("...and the card position is measured, not guessed",
          "_ptLayoutHero" in html)
    # The reduce-motion rule used to force the scrim opaque, which painted
    # the page background over the whole map for anyone with that setting on.
    # The remaining reduce-motion block (skeleton pulse, route transitions)
    # is fine and stays; what must never come back is anything that pins the
    # scrim or the reveal to full opacity.
    check("reduce-motion no longer buries the map",
          ".scroll-scrim { opacity: 1 !important; }" not in html)
    check("...and the script has no reduce-motion bail-out either",
          "matchMedia('(prefers-reduced-motion: reduce)')" not in html)


def test_flight_sheet():
    """The schedule lives in a fixed sheet with its own scrollbar. (1.12.0)

    Replaces test_scroll_reveal. The scroll-driven reveal drove the scrim,
    the schedule, the card's height, the map's framing and the heads-up
    controls from ONE number — the page's scroll offset — and three
    releases went on what happened when they disagreed. The sheet is
    furniture; nothing else is measured against the page's scroll because
    the page no longer scrolls.

    The three checks that matter here are inherited from real failures and
    must survive any redesign, so they are restated rather than dropped.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()

    check("the sheet exists", 'class="sheet" id="sheet"' in html)
    # One height only since 1.14.1: --sheet-full went with the drag.
    check("...and rests PEEKING, not shut", "--sheet-peek" in html)
    check("...with its own scrollbar", ".sheet-body {" in html and "overflow-y: auto" in html)
    check("...that a flick cannot leak out of", "overscroll-behavior: contain" in html)

    # LESSON FROM v6.1: the schedule was hidden in CSS with the script
    # relied on to bring it back, so a 404 on Leaflet took the schedule
    # down with the map. Here the equivalent risk is killing the document's
    # scrollbar from CSS: if the script then dies, there is no sheet
    # scrolling AND no page scrolling, and the schedule is unreachable.
    check("the page's scrollbar is not killed by CSS alone",
          "body.has-sheet { overflow: hidden; }" in html
          and "\n    body { overflow: hidden" not in html)
    check("...the script opts in only once it is alive",
          "document.body.classList.add('has-sheet');" in html)

    # LESSON FROM v6.2: the reveal lived inside the map's IIFE, so a
    # missing Leaflet took the schedule down with the map.
    sheet_at = html.find("// ---- THE FLIGHT SHEET")
    map_at = html.find("const mapEl = document.getElementById('flight-map')")
    check("the sheet does not live inside the map block",
          sheet_at != -1 and map_at != -1 and sheet_at < map_at)
    check("a missing Leaflet is caught, not thrown",
          "typeof L === 'undefined'" in html)
    check("...and says so instead of showing a blank", "Map unavailable" in html)

    # THE POINT OF THE WHOLE EXERCISE: the list starts on the current
    # flight. Impossible in the old layout without moving flown legs above
    # the card and re-deriving the card's position with them.
    check("the list starts on the current flight",
          "function startAtCurrent()" in html)
    check("...falling back to the landmark when nothing is live",
          "document.getElementById('past-anchor')" in html)
    check("...and re-runs once late layout settles",
          "window.setTimeout(startAtCurrent, 400)" in html)

    # The card was ALREADY bottom-anchored; it just had the wrong thing to
    # measure against. If this regresses, the card overlaps the sheet.
    # The card that parked against the sheet is gone (1.13.0); the detail
    # panel is fixed to the bottom of the screen and measures nothing.
    check("nothing is parked against the sheet any more",
          "function tabTop()" not in html)

    # Dragging must be taken from the GRIP only. Listening on the sheet
    # would steal every scroll gesture inside the list.
    check("no drag handlers survive on the grip",
          "grip.addEventListener('touchstart'" not in html)
    check("nothing sets an inline height on the sheet",
          "sheet.style.height" not in html)

    # The scrim is gone entirely, not merely faded.
    code = re.sub(r"^\s*//.*$", "", html, flags=re.M)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    code = re.sub(r"\{#.*?#\}", "", code, flags=re.S)
    check("the scrim is gone", "scroll-scrim" not in code)
    check("...and so is the map shield it needed", "map-shield" not in code)
    check("reduce-motion still cannot bury the map",
          ".scroll-scrim { opacity: 1 !important; }" not in html)


def test_tapping_a_row_is_the_only_way_in():
    """Show on map is gone; a row tap does both. (1.14.1)

    It selected a leg and drew it on the map — which is now what tapping
    the row itself does, on the way to opening the panel. A second control
    for one action is a second thing to keep in step, and this was the one
    that spent five releases dead outside every script block (1.12.0).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    code = re.sub(r"^\s*//.*$", "", html, flags=re.M)
    code = re.sub(r"\{#.*?#\}", "", code, flags=re.S)
    check("the link is gone", "data-show-on-map" not in code)
    check("the row dropdown is gone", 'class="row-detail"' not in code)
    check("...and the caret that opened it",
          "row-caret" not in re.sub(r"/\*.*?\*/", "", code, flags=re.S))
    check("a row tap still selects the leg", "window._ptSelectLeg(id)" in html)
    check("...and there is one selection path",
          html.count("function selectLeg(legId, opts)") == 1)


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

def test_viewer_theme_is_consistent_across_pages():
    """A viewer's theme lives in a cookie. The tracker applied it inline and
    the calendar forgot to, so one person got a light tracker and a dark
    calendar."""
    from types import SimpleNamespace as _NS
    from app.main import viewer_display_overrides as _vdo

    class _Req:
        def __init__(self, c): self.cookies = c

    base = {"theme": "dark", "time_format": "24",
            "show_flightaware": True, "show_fr24": True}
    check("a viewer's cookie overrides the pilot's theme",
          _vdo(_Req({"pt_viewer_theme": "light"}), None, base)["theme"] == "light")
    check("...but never overrides the pilot's own",
          _vdo(_Req({"pt_viewer_theme": "light"}), {"id": 1}, base)["theme"] == "dark")
    check("...and falls back when unset",
          _vdo(_Req({}), None, base)["theme"] == "dark")
    check("the clock format follows the same path",
          _vdo(_Req({"pt_viewer_tf": "12"}), None, base)["time_format"] == "12")

    # EVERY PAGE A VIEWER CAN REACH GOES THROUGH THE ONE HELPER, AND
    # RESOLVES IT BEFORE FORMATTING ANYTHING.
    #
    # This used to look for the helper's name near the render call, which
    # the calendar satisfied while still being wrong: it applied the
    # override on the way OUT, to hand the template a theme, after every
    # time on the page had already been formatted from the PILOT's
    # time_format. A viewer on a 12-hour clock got a light calendar full
    # of 24-hour times. The check now asks the question that catches
    # that — is the pilot's raw setting used ANYWHERE in the route.
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "app", "main.py"), encoding="utf-8") as fh:
        src = fh.read()

    for marker, end, name in (
            ("async def calendar_page", 'jinja_env.get_template("calendar.html")', "calendar"),
            ("async def viewer_page", 'jinja_env.get_template("viewer.html")', "tracker")):
        if marker not in src:
            continue
        body = src[src.index(marker):]
        body = body[:body.index(end)] if end in body else body[:6000]
        check(f"the {name} resolves viewer overrides",
              "viewer_display_overrides" in body)
        # CODE ONLY. A comment recording what the bug WAS must not read as
        # the bug still being there — otherwise documenting a fix is what
        # breaks the test that proves it, and the note gets deleted to get
        # to green. Same rule as code_only() in the strip tests.
        body = re.sub(r"#.*$", "", body, flags=re.M)
        body = re.sub(r'"""(.*?)"""', "", body, flags=re.S)
        # The raw pilot setting must not survive alongside the override.
        # Reading it is how the two fell out of step.
        check(f"...and the {name} never formats from the pilot's clock",
              "settings.time_format" not in body,
              "settings.time_format still read")
        check(f"...nor paints from the pilot's theme",
              "settings.theme" not in body, "settings.theme still read")


def test_detail_panel_replaces_the_hero_card():
    """Three hero-layout tests collapse into this one. (1.13.0)

    They guarded a card that floated over the map and grew upward from a
    bottom edge welded above the tab pills: that it could not outgrow the
    screen, that opening it disturbed neither map nor schedule, that its
    bottom edge did not move. Every one of those is a property of a
    POSITION THAT HAD TO BE COMPUTED — a measured spacer, recomputed on
    resize, rotation, every ResizeObserver tick and every open and close.

    The panel is fixed to the bottom of the screen and moved with a
    transform. It cannot outgrow the screen because its height is the
    sheet's height; it cannot disturb the map because it shares no
    coordinate system with it; its bottom edge cannot drift because it is
    bottom: 0. The bugs those tests were written after are not fixed so
    much as made unreachable, so the tests are replaced rather than
    adapted.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    css_block = html.split(".detail-panel {", 1)[1].split("}", 1)[0]

    check("the panel is fixed to the bottom", "bottom: 0" in css_block, css_block)
    # THE PANEL IS THE SHEET'S HEIGHT, NOT ITS CONTENT'S (1.17.0). It
    # carried max-height and no height, so it sized to whatever was in it:
    # a leg with a gate, a closeout and live ADS-B came up nearly full
    # screen, and the same leg before pushback came up a couple of inches.
    # Same tap, two different windows. Tying it to the sheet means opening
    # a flight and closing it again moves nothing on screen.
    check("...and stands exactly as tall as the list it covers",
          "height: var(--sheet-peek)" in css_block, css_block)
    check("...so it cannot size itself to its contents",
          "max-height" not in css_block, css_block)
    check("...and scrolls inside when the content is longer",
          "overflow-y: auto" in css_block, css_block)
    # transform, not height. Every height-driven animation in this file has
    # cost a bug, twice over the same one.
    check("it slides on a transform, not a height",
          "transform: translateY(101%)" in css_block
          and "transition: transform" in css_block, css_block)
    check("...and is hidden from tab order when shut",
          "visibility: hidden" in css_block, css_block)
    check("...becoming visible only when open",
          ".detail-panel.open { transform: translateY(0); visibility: visible; }" in html)
    check("reduced motion skips the slide",
          ".detail-panel { transition: none; }" in html)

    # THE ENGINE IS GONE, not merely unused.
    code = re.sub(r"^\s*//.*$", "", html, flags=re.M)
    code = re.sub(r"\{#.*?#\}", "", code, flags=re.S)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    for dead in ("function capPanel", "function minTop", "function slidePanel",
                 "function foldEnds", "function measureEnds", "function setExpanded",
                 "hero-space", "expand-hint", "card-more"):
        check("removed: " + dead, dead not in code, dead)
    check("the card's own box rule is gone", ".collapsed-card {" not in code)

    # A modal with no way out is worse than no modal.
    check("the X closes it", 'id="detail-close"' in html)
    check("...and so does Escape", "e.key === 'Escape'" in html)

    # AUTO-OPEN: once, on load, and never again. The poller runs every few
    # seconds; a panel that reappears each time is a fight, not a feature.
    check("it auto-opens when a leg is airborne", "data-live-leg" in html)
    check("...from the same flag the live ADS-B box uses",
          "data-live-leg=\"{{ '1' if is_selected_live else '0' }}\"" in html)
    check("...exactly once, not on every poll",
          html.count("panel.classList.add('open')") == 1
          or html.count("classList.add('open')") <= 2)
    check("...and it is the SAME panel a tap opens",
          html.count('id="detail-panel"') == 1)

    # A drag has to be meant, or every tap on the handle nudges the sheet.
    # The drag is gone (1.14.1): two snap points plus a drag was three
    # ways to end up somewhere you did not mean. The sheet is fixed and
    # the list scrolls inside it.
    check("the sheet does not move", "sheet.classList.toggle('open'" not in html)
    check("...and has no second height to snap to", ".sheet.open {" not in html)


def test_detail_panels_slide_rather_than_snap():
    """Both expanding panels animate their height. The spacing has to live
    on an inner wrapper: with padding and a border on the outer element, a
    height of 0 still renders ~29px tall, so the panel would jump open by
    that much and only then slide."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()

    check("the card's panel has a height transition",
          ".expand-details.animating { transition: height" in html)
    check("...and clips its contents while it moves",
          ".expand-details { display: none; overflow: hidden; }" in html)
    check("...with the spacing moved onto an inner wrapper",
          ".ed-inner {" in html and 'class="ed-inner"' in html)

    check("the flight-list rows animate too",
          ".row-detail.animating { transition: height" in html)
    check("...and clip while moving",
          ".row-detail { overflow: hidden; }" in html)
    check("...with their spacing on .row-detail-body",
          ".row-detail-body {" in html)

    # These were behind prefers-reduced-motion, which meant the slide simply
    # did not happen on a phone with Reduce Motion switched on -- the panel
    # snapped open exactly as it had before the animation was written. The
    # animation was asked for explicitly, so it now always runs.
    check("the slide is not suppressed by reduced motion",
          "@media (prefers-reduced-motion: reduce) {\n      .expand-details.animating"
          not in html)
    check("...nor is the spacer that moves with it",
          "@media (prefers-reduced-motion: reduce) {\n      .hero-space.animating"
          not in html)
    check("...nor the flight-list rows",
          "@media (prefers-reduced-motion: reduce) {\n      .row-detail.animating"
          not in html)
    check("and the scrim is still never pinned opaque",
          ".scroll-scrim { opacity: 1 !important; }" not in html)

    # Height is released back to auto so late-arriving detail is not clipped.
    check("the row panels are released back to auto",
          "panel.style.height = '';" in html)


def test_schedule_works_without_the_map():
    """renderLegDetail/toggleLegDetail and their click handlers used to sit
    inside the map block, BELOW its `typeof L === 'undefined'` bail-out. A
    failed Leaflet download therefore killed the flight list too: rows still
    looked tappable, none of them opened. They live in their own block now."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    bail = html.index("typeof L === 'undefined'")
    for fn in ("function renderLegDetail(", "function toggleLegDetail(",
               "function timeLineHTML("):
        check("%s is defined before the Leaflet bail-out" % fn.split('(')[0].split()[-1],
              html.index(fn) < bail,
              "found at %d, bail-out at %d" % (html.index(fn), bail))
    # The row-detail dropdown and Show-on-map both went in 1.14.1; a row
    # tap now opens the shared panel. The LESSON stands and moves to the
    # handler that replaced them: it must not sit inside the map block, or
    # a missing Leaflet takes the schedule's only interaction with it.
    check("the row tap handler is outside the map block",
          html.index("sheet.addEventListener('click'") < bail)
    check("the shared time formatter is published for the map block",
          "window._ptTimeLineHTML" in html)


def test_session_key_survives_redeploy():
    """The sign-out bug. The key lived only in data/secret_key.txt; if that
    file went missing during a deploy, every cookie stopped verifying and
    everyone was logged out. It now lives in the database."""
    import importlib, tempfile as _tf
    from pathlib import Path as _P
    import app.auth as _auth
    d = _tf.mkdtemp()
    original = _auth.SECRET_KEY_FILE
    try:
        _auth.SECRET_KEY_FILE = _P(d) / "secret_key.txt"
        first = _auth.get_or_create_secret_key()
        check("a session key is produced", len(first) >= 32)
        check("...and is stable when asked twice",
              _auth.get_or_create_secret_key() == first)
        # Simulate the deploy losing the loose file.
        _auth.SECRET_KEY_FILE.unlink(missing_ok=True)
        check("...and survives the key file being wiped by a deploy",
              _auth.get_or_create_secret_key() == first)
        # An explicit pin always wins, so it can be recovered by hand.
        os.environ["PT_SECRET_KEY"] = "p" * 64
        check("...and an env pin overrides everything",
              _auth.get_or_create_secret_key() == "p" * 64)
    finally:
        os.environ.pop("PT_SECRET_KEY", None)
        _auth.SECRET_KEY_FILE = original


def test_scheduled_time_line_is_marked_as_an_echo():
    """A future leg's dropdown printed the scheduled time a second time,
    directly under the row that already showed it. The fields that let the
    UI tell an echo from real news have to reach the client."""
    from app.view import _time_line, _variance
    base = datetime(2026, 7, 1, 17, 34, tzinfo=timezone.utc)

    unflown = _time_line(None, base, "America/Chicago", "24")
    check("an unflown leg is tagged as merely scheduled",
          unflown["source"] == "scheduled", str(unflown))
    check("...with no variance to report", unflown["minutes"] == 0, str(unflown))
    check("...and is not settled", unflown["settled"] is False, str(unflown))

    flown = _time_line(
        _variance(base, (base + timedelta(minutes=12)).isoformat(), None, None,
                  "America/Chicago", "24", "Departing", "Departed"),
        base, "America/Chicago", "24")
    check("a real airline time carries its source", flown["source"] == "airline",
          str(flown))
    check("...its variance in minutes", flown["minutes"] == 12, str(flown))
    check("...and counts as settled", flown["settled"] is True, str(flown))


def test_two_letter_zones():
    from app.main import tz_abbr
    from app.view import _TWO_LETTER_ZONE   # moved: one shared copy, see view.py
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


def test_flight_strip_is_one_component():
    """ONE way to draw a flight, in three sizes. (1.9.0)

    The tracker card, the flight list and the calendar agenda each drew a
    flight differently, which is the same failure the colour palette had
    before v5.9 and the zone label had before 1.2.0: three implementations
    of one idea, drifting independently, and a fix applied to whichever one
    somebody happened to be looking at.

    So the checks below are not about how it looks. They are about the
    component staying SINGLE: living in the shared stylesheet, scaling by
    custom property rather than by duplicated rules, and taking its glyphs
    from one file each.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "static", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()

    # These checks look for RULES. A comment recording what was deleted,
    # and why, must not read as the deleted thing still being there — that
    # would make documenting a removal impossible.
    def code_only(text):
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)   # CSS
        text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)   # Jinja
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)  # HTML
        return re.sub(r"^\s*//.*$", "", text, flags=re.M)   # JS line comments

    html_code, css_code = code_only(html), code_only(css)

    check("the strip lives in the SHARED stylesheet", ".fstrip {" in css_code)
    for size in ("lg", "md", "sm"):
        check(f"...and declares a --{size} size", f".fstrip--{size} {{" in css_code)
    # Using the class is the point; DECLARING a size in a template is the
    # regression — that is how eleven copies of the palette happened. A
    # contextual override (.fstrip-head .status) is fine and stays with
    # the surface that needs it; a redeclared .fstrip or .fstrip--lg is not.
    #
    # MATCHED AT THE HEAD OF A SELECTOR ONLY (1.19.0). This used to look
    # for the string anywhere, which meant it did not enforce the rule
    # above — it enforced a stricter one nobody agreed to, and would have
    # rejected exactly the contextual override the comment permits. It
    # passed only because viewer.html happened not to have one. The
    # calendar grew one in 1.18.0 (`.cal-leg-head > .fstrip`) and tripped
    # the identical check there, which is how this was found.
    bare = re.search(r"(?:^|[,{}])\s*\.fstrip(--\w+)?\s*\{", html_code, re.M)
    check("no template redeclares the strip or its sizes", bare is None,
          bare.group(0) if bare else "")
    # If a size modifier ever has to restate a layout rule rather than a
    # variable, the component has stopped being one component.
    for size in ("lg", "md", "sm"):
        block = css_code.split(f".fstrip--{size} {{", 1)[1].split("}", 1)[0]
        decls = [d.split(":")[0].strip() for d in block.split(";") if ":" in d]
        check(f"--{size} overrides variables only, never layout",
              all(d.startswith("--fstrip-") for d in decls),
              ", ".join(d for d in decls if not d.startswith("--fstrip-")))

    # One glyph file each, sized from the strip's own variable. Attributes
    # on the svg would pin all three sizes to one pixel count — the same
    # trap that produced three different aeroplanes before 1.7.0.
    for name in ("arrow_out.html", "arrow_in.html"):
        path = os.path.join(here, "templates", "partials", name)
        check(f"{name} exists as a shared partial", os.path.exists(path))
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                svg = fh.read()
            # stroke-width is a paint property and is fine; a bare
            # width/height on the <svg> is what would pin all three sizes
            # to one pixel count.
            check(f"...{name} carries no fixed width/height",
                  not re.search(r'(?<!stroke-)\b(width|height)="', svg))
    check("the card includes the shared glyphs rather than inlining them",
          'partials/arrow_out.html' in html_code and 'partials/arrow_in.html' in html_code)
    check("the glyph is sized from --fstrip-glyph",
          "width: var(--fstrip-glyph)" in css_code)

    # THE COLOUR RULE (owner's, 1.9.0): green EARLY, red LATE, plain for
    # exactly-as-scheduled AND for nothing-published-yet. On time is not
    # green — green has to mean "better than the plan" or it becomes the
    # background colour of the app, and it would also make "the airline
    # says on time" indistinguishable from "the airline has said nothing",
    # when only one of those is a report.
    check("early is green", ".fstrip-time.early { color: var(--good); }" in css_code)
    check("late is red", ".fstrip-time.late { color: var(--bad); }" in css_code)
    check("on time and unreported are BOTH plain, not green",
          ".fstrip-time.scheduled, .fstrip-time.ontime { color: var(--text); }"
          in css_code)
    check("...so nothing paints an on-time time green",
          not re.search(r"\.fstrip-time\.ontime[^{]*\{[^}]*--good", css_code))

    # The disc takes its colour from ITS OWN time, not from which end it
    # is. Fixed-by-direction discs meant a red disc could sit beside a
    # green time and read as a contradiction.
    check("the disc is not coloured by direction",
          not re.search(r"\.fstrip-disc\.(dep|arr)\s*\{", css_code))
    check("...it is coloured by state: early green",
          ".fstrip-disc.early { background: var(--good); color: #fff; }" in css_code)
    check("...late and cancelled red",
          ".fstrip-disc.late, .fstrip-disc.cancelled { background: var(--bad); color: #fff; }"
          in css_code)
    check("...and a disc with no news is still visible",
          "background: var(--border); color: var(--muted);" in css_code)
    # The panel header lost its times row in 1.13.0 — the airport blocks
    # below state both times at three times the size, which is what the
    # folding row of 1.10.1 was working around. The disc/time pairing is
    # still asserted, on the surfaces that still have one: the list rows.
    check("list rows pair each disc with its own time's state",
          "leg.dep_line.state" in html_code and "leg.arr_line.state" in html_code)
    # One function writes BOTH halves, so they cannot drift apart.
    check("the poller repaints disc and time together",
          "disc.className = 'fstrip-disc' + state" in html_code)

    # The collapsed strip shows the CORRECTED time and nothing else. The
    # struck-through original and the "12 min late" note belong in the
    # expanded view, where there is room to say it in words.
    check("no delay chip survives on the strip", 'class="chip-delay' not in html_code)
    check("no struck-through original on any strip",
          "fstrip-was" not in html)

    # Superscript zones, the 1.3.0 rule, now reachable because the payload
    # emits the zone separately from the time.
    # The panel header no longer carries times (1.13.0), so the surfaces
    # to check are the airport blocks and the list rows.
    for tid in ("v-dep-time", "v-arr-time"):
        seg = html.split('id="%s"' % tid, 1)[1][:400]
        check("%s carries its zone as a superscript element" % tid,
              'class="tz"' in seg)

    # The classes the old card used are GONE, not merely unused. Half a
    # deleted design left in the stylesheet is how somebody restores it.
    # Checked as RULE declarations, so the comment that records what was
    # removed (and why) does not read as the thing itself.
    for dead in ("chip-time", "chip-delay", "route-ends", "route-end",
                 "route-code", "route-time-wrap", "city-route", "flight-num",
                 "flight-line"):
        check(f"dead rule removed: .{dead}",
              not re.search(r"\.%s\s*[,.{:]" % re.escape(dead), html_code)
              and not re.search(r"\.%s\s*[,.{:]" % re.escape(dead), css_code),
              dead)


def test_time_line_splits_the_zone_off():
    """`time` keeps the glued form; `time_short` + `zone` are the parts.

    A glued "12:39 CDT" cannot be superscripted — the zone sits inside the
    same text node as the digits, so CSS has nothing to select. That, and
    not a forgotten template, is why the expanded card still printed
    full-size inline zones two releases after 1.3.0 superscripted every
    other surface.
    """
    from app.view import _time_line
    from datetime import datetime as _dt, timezone as _tz

    base = _dt(2026, 8, 16, 22, 30, tzinfo=_tz.utc)
    line = _time_line(None, base, "America/Chicago", "24")
    check("a scheduled line still returns a glued `time`",
          line and " " in (line["time"] or ""))
    check("...and the bare time separately", line and line["time_short"] == "17:30")
    check("...and the zone separately", line and line["zone"] == "CT")
    check("...with no zone inside time_short",
          line and not any(c.isalpha() for c in line["time_short"]))

    var = {"time": "18:00 CDT", "original": "17:30 CDT", "time_short": "18:00",
           "original_short": "17:30", "state": "late", "short_text": "30 min late",
           "source": "airline", "minutes": 30, "settled": True}
    line = _time_line(var, base, "America/Chicago", "24")
    check("a revised line carries the corrected time bare", line["time_short"] == "18:00")
    check("...and the original bare, for the strike-through",
          line["was_short"] == "17:30")
    check("...and one zone for the pair", line["zone"] == "CT")

    # A time that did not move must not be struck through against itself.
    same = dict(var, time="17:30 CDT", original="17:30 CDT",
                time_short="17:30", state="ontime")
    line = _time_line(same, base, "America/Chicago", "24")
    check("an unmoved time offers nothing to strike through",
          line["was"] is None and line["was_short"] is None)

    # The label answers daylight time for the DATE BEING SHOWN. A January
    # leg through the same airport is not summer.
    winter = _dt(2026, 1, 16, 22, 30, tzinfo=_tz.utc)
    check("the zone is resolved against the leg's own date, not today",
          _time_line(None, winter, "Europe/London", "24")["zone"] == "GMT"
          and _time_line(None, base, "Europe/London", "24") is not None)


def test_leg_switch_keeps_the_time_rows():
    """Tapping a flight must not blank the two rows that matter. (1.9.0)

    applyEnrichment hides Departure and Arrival when dep_line/arr_line are
    absent, and applyLegPayload was calling it without them — so switching
    legs wiped the expanded view's two most important rows, and only a full
    page reload brought them back. Silent: every other row was fine, so it
    read as "not loaded yet" rather than as a bug.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    call = html.split("function applyLegPayload", 1)[1]
    call = call.split("applyEnrichment({", 1)[1].split("});", 1)[0]
    check("the leg switch passes dep_line through", "dep_line:" in call)
    check("...and arr_line", "arr_line:" in call)

    # And the strip's own times are rebuilt from those lines rather than
    # set as flat text, which would flatten the superscript zone away.
    check("strip times are rebuilt, not setText'd",
          "applyStripTime('card-dep-time'" in html
          and "setText('card-dep-time'" not in html)
    check("...from the *_line pair, which always exists",
          "'card-dep-disc', data.dep_line" in html)


def test_expanded_view_is_per_airport():
    """One box per AIRPORT, not a column of label/value pairs. (1.10.0)

    The old shape scattered one airport's story across four non-adjacent
    rows — "Arrival" near the top, "XNA gate" two rows down, "Baggage"
    below that — and left the reader to reassemble it. Nobody asks "what is
    the arrival time"; they ask "what do I need to know about XNA".
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    with open(os.path.join(here, "static", "app.css"), encoding="utf-8") as fh:
        css = fh.read()

    def code_only(text):
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        return re.sub(r"^\s*//.*$", "", text, flags=re.M)

    html_code, css_code = code_only(html), code_only(css)

    check("the departure has its own block", 'id="apt-dep"' in html_code)
    check("the arrival has its own block", 'id="apt-arr"' in html_code)
    check("the block lives in the SHARED stylesheet", ".aptblock {" in css_code)
    check("no template declares its own .aptblock rules",
          not re.search(r"\.aptblock[-\w]*\s*\{", html_code))

    # THE OWNER'S RULE: lateness in WORDS only under the arrival. The
    # departure keeps its tint and its struck-through original — nothing
    # is concealed — but a leg that pushed twelve late and lands on time
    # is not a late flight, and spelling it out invites reading it as one.
    check("the arrival narrates its lateness", 'id="v-arr-note"' in html_code)
    check("the departure does NOT", 'id="v-dep-note"' not in html_code)
    check("...and the poller honours the same rule",
          "applyAptBlock('dep', data.dep_line, false)" in html_code
          and "applyAptBlock('arr', data.arr_line, true)" in html_code)
    check("...enforced by one flag, not two code paths",
          html_code.count("function applyAptBlock") == 1)

    # Both ends still carry COLOUR. Removing the words must not have
    # removed the tint with them.
    check("the departure time is still tinted", 'id="v-dep-time"' in html_code
          and "aptblock-time' + state" in html_code)
    check("the departure still shows what it moved from",
          'id="v-dep-was"' in html_code)

    # The struck-through original, which is what the expanded view is FOR.
    check("struck-through original is struck through",
          "text-decoration: line-through" in css_code.split(".aptblock-was")[1][:200])

    # Rows removed by owner's decision.
    check("Closed out is gone from the card", "row-closed" not in html_code)
    check("...and so is its value element", "v-closed" not in html_code)
    check("Arrival time from survives", 'id="row-arrsrc"' in html_code)

    # The panel shows on ONE condition. Template and poller disagreeing
    # about when an element is visible is how a leg with a perfectly good
    # scheduled time rendered an empty panel until the first poll.
    server = html.split('id="flight-detail"', 1)[1].split(">", 1)[0]
    check("template and poller agree on when the panel shows",
          "current.dep_line or current.arr_line or current.gates" in server
          and "!!(data.dep_line || data.arr_line || data.gates)" in html_code)

    check("the dead applyTimeLine helper is gone",
          "function applyTimeLine" not in html_code)
    check("...but the flight list's shared formatter survives",
          "_ptTimeLineHTML" in html_code)
    check("no detail block carries a heading", "<h3" not in html_code)


def test_route_facts_are_not_measurements():
    """Block time and route distance are schedule/map facts, not live ones.

    Invariant 9 blanks the LIVE figures — percent en route, distance to go,
    ETE — without a position fix. These two are different in kind: the
    great-circle distance between two fixed points and the block time the
    bid line allows are the same before pushback, in the cruise and after
    closure. That is exactly why they are safe to print beside figures
    that go blank, and why they must never be computed from a fix.
    """
    from app.main import _route_nm, _block_time
    from app.airports import enrich_leg
    from app.models import FlightLeg
    from datetime import date as _date

    l = FlightLeg(id="R1", date=_date(2026, 8, 16), flight_number="3729",
                  origin="DFW", destination="OKC",
                  dep_time_local="06:00", arr_time_local="07:22")
    enrich_leg(l)
    nm = _route_nm(l)
    check("DFW-OKC is roughly 150 nm", nm and 130 < nm < 180, str(nm))
    check("block time is read off the schedule", _block_time(l) == "1h 22m",
          str(_block_time(l)))

    # A leg CROSSING A ZONE must not have its block time computed by
    # subtracting one wall clock from the other — that is the ANC-NRT bug
    # of 1.1.0 in a different place. LAX 22:00 to JFK 06:20 next day is
    # five hours twenty in the air, not eight.
    j = FlightLeg(id="R2", date=_date(2026, 8, 16), flight_number="12",
                  origin="LAX", destination="JFK",
                  dep_time_local="22:00", arr_time_local="06:20")
    enrich_leg(j)
    bt = _block_time(j)
    check("a zone-crossing leg gets the time actually flown",
          bt == "5h 20m", str(bt))

    # An airport the database does not know cannot produce a distance, and
    # must produce nothing rather than a zero.
    u = FlightLeg(id="R3", date=_date(2026, 8, 16), flight_number="1",
                  origin="ZZZZ", destination="QQQQ",
                  dep_time_local="06:00", arr_time_local="07:00")
    enrich_leg(u)
    check("an unknown airport yields no distance, not zero",
          _route_nm(u) is None, str(_route_nm(u)))


def test_arrival_source_is_in_english():
    """The internal token is this app's vocabulary, not the reader's.

    `observed` means the app watched the aeroplane stop; `estimated` means
    nobody has confirmed anything. The person most likely to be reading
    this row is the one least equipped to guess. Translated on the SERVER
    so the page and the poll cannot word it differently.
    """
    from app.view import ARRIVAL_SOURCE_TEXT
    # LABELS, NOT SENTENCE FRAGMENTS (1.20.0). These used to read "the
    # airline" / "an estimate", which only worked while the sole place
    # they appeared completed the phrase "Arrival time from ...". They
    # are shown as a VALUE beside a key — in a two-column row on the
    # tracker, and again in the calendar's history panel since 1.18.0 —
    # where a lower-case fragment starting with "the" reads as a typo.
    check("airline reads as a label",
          ARRIVAL_SOURCE_TEXT["airline"] == "Airline")
    check("observed still says whose tracking it is",
          ARRIVAL_SOURCE_TEXT["observed"] == "Our own tracking")
    check("estimated is still admitted as an estimate",
          ARRIVAL_SOURCE_TEXT["estimated"] == "Estimate")
    check("none of them is a sentence fragment",
          all(v[:1].isupper() and not v.lower().startswith(("the ", "an ", "a "))
              for v in ARRIVAL_SOURCE_TEXT.values()),
          str(list(ARRIVAL_SOURCE_TEXT.values())))
    # The distinction is kept, not smoothed away: the logbook export (N3)
    # may use only airline-confirmed times, so the card must not present
    # the three as interchangeable.
    check("the three stay distinguishable",
          len(set(ARRIVAL_SOURCE_TEXT.values())) == 3)
    check("no internal token leaks through as-is",
          not any(v == k for k, v in ARRIVAL_SOURCE_TEXT.items()))


def test_live_box_does_not_swallow_the_flight_detail():
    """#flight-detail was a CHILD of #live-section. (1.10.1)

    Invisible in the source — the indentation showed them as siblings and
    they read as siblings — but applyLegPayload hides #live-section
    whenever the selected leg is not the live one. So tapping any past or
    future flight and opening the card gave a completely empty panel: no
    times, no gates, no airport blocks. Nothing pointed at the cause,
    because the code doing the hiding names only the live box.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()

    # Walk div depth through the panel and record where each block opens.
    body = html.split('<div class="expand-details open"', 1)[1]
    depth, opens = 0, {}
    for line in body.split("\n"):
        for key, marker in (("live", 'id="live-section"'),
                            ("detail", 'id="flight-detail"')):
            if marker in line and key not in opens:
                opens[key] = depth
        depth += len(re.findall(r"<div\b", line)) - len(re.findall(r"</div>", line))
        if depth < 0:
            break
    check("both blocks were found", "live" in opens and "detail" in opens, str(opens))
    check("the live box does NOT contain the flight detail",
          opens.get("live") == opens.get("detail"), str(opens))
    # And the ADS-B numbers come SECOND: the panel opens right under the
    # progress bar, so whatever is first is what the reader lands on.
    # Altitude is the pilot's number; arrival time is everyone else's.
    check("flight detail is above the ADS-B box",
          body.index('id="flight-detail"') < body.index('id="live-section"'))


def test_list_dropdown_follows_the_same_decisions():
    """The flight list has its OWN renderer, and it was missed. (1.10.1)

    renderLegDetail builds a second label/value list in JavaScript. The
    1.10.0 decisions — drop "Closed out", say the arrival source in
    English — were applied to the card only, so the deploy looked like it
    had not happened. Full move to the shared component is 1.11.0.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    code = re.sub(r"^\s*//.*$", "", html, flags=re.M)
    code = re.sub(r"\{#.*?#\}", "", code, flags=re.S)
    check("no surface renders Closed out any more", "'Closed out'" not in code)
    check("the list dropdown says the source in English",
          "d.arrival_source_text" in code)
    check("...and no longer prints the raw token",
          "esc(d.arrival_source)" not in code)


def test_map_cannot_steal_the_scroll():
    """Superseded by the sheet, and the guarantee is now stronger. (1.12.0)

    1.10.2 added .map-shield: an invisible sheet over the map below the
    card, because the map is a fixed full-screen layer and Leaflet takes a
    touch through any transparent gap, so scrolling toward next week's
    flights sometimes panned the map instead.

    The sheet removes the problem at its root rather than covering it. The
    document does not scroll at all now — the only scrollable region on the
    page is inside the sheet, and the sheet is opaque. There is no page
    scroll left for the map to steal, so the shield is deleted rather than
    kept as belt and braces: an invisible full-width element that swallows
    taps is not something to leave lying around once its reason is gone.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    code = re.sub(r"^\s*//.*$", "", html, flags=re.M)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    code = re.sub(r"\{#.*?#\}", "", code, flags=re.S)

    check("the shield is gone", "map-shield" not in code)
    check("the document no longer scrolls", "body.has-sheet { overflow: hidden; }" in html)
    check("...and the only scroller is the sheet's own body",
          ".sheet-body {" in html and "overflow-y: auto" in html)
    # The strip of map ABOVE the card is still pannable — that was true of
    # the shield too, and it is the part you can actually see.
    check("the visible map is still live",
          'id="flight-map"' in html)


def test_refit_glides_rather_than_snapping():
    """A re-fit is a correction, not a new subject. (1.10.2)

    The route has not moved; only the window onto it has, because the card
    changed height. Leaflet's fitBounds is instant by default, which turns
    that correction into a jump that reads as the map losing its place.
    The first fit still snaps — there is no previous view to ease from.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    check("card-driven re-fits ask for a glide",
          "fitToPoints(lastFitPts, true)" in html)
    check("...and fitToPoints honours it",
          "opts.animate = true; opts.duration = 0.35;" in html)
    check("...including the single-point case",
          "glide ? { animate: true, duration: 0.35 } : undefined" in html)
    # Only the card-driven correction glides. The first paint and the
    # poll-driven fits stay instant, which is why the flag is opt-in
    # rather than the default.
    check("the initial fit is still instant",
          "function fitToPoints(pts, glide)" in html
          and "window.requestAnimationFrame(function() { fitToPoints(lastFitPts); });" in html)
    check("...and only _ptRefit opts into the glide",
          html.count("fitToPoints(lastFitPts, true)") == 1)


def test_fold_and_refit_machinery_is_gone():
    """Both are unreachable now, so both are deleted. (1.13.0)

    1.10.1 folded the strip's times away when the panel opened and taught
    the spacer maths about it; 1.10.2 stopped the map re-fitting twice per
    collapse. Both were corrections to a card that floated over the map,
    changed height as it opened, and had its position recomputed from that
    height. The detail panel is fixed to the bottom of the screen, moved
    with a transform, and carries no times in its header at all.

    Kept as a test rather than dropped because the cheapest way to
    reintroduce those bugs is to reintroduce the machinery.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    with open(os.path.join(here, "static", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    code = re.sub(r"^\s*//.*$", "", html, flags=re.M)
    code = re.sub(r"\{#.*?#\}", "", code, flags=re.S)
    css_code = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    for dead in ("endsH", "foldEnds", "measureEnds", "_ptSliding", "cardBase"):
        check("gone: " + dead, dead not in code, dead)
    check("the folding rule is gone from the stylesheet",
          ".collapsed-card.expanded .fstrip--lg .fstrip-ends" not in css_code)
    # The strip itself must NOT have gained a fixed height on the way out;
    # that was only ever there to make the fold measurable.
    ends = css_code.split(".fstrip-ends {", 1)[1].split("}", 1)[0]
    check("the ends row has no leftover height cap",
          "max-height" not in ends, ends)


def test_tracker_is_scoped_to_one_trip():
    """The tracker holds ONE trip. Nothing else. (1.16.0)

    It used to render the entire 365-day roster and hide most of it behind
    a button — a list that grows without bound, pretending to be a list
    that does not. 1.11.0 cut that to this trip and the next; 1.16.0 cuts
    the next one too, because appending it put a second "Day 1" under the
    first trip's last overnight and the list read as one unbroken run of
    days that silently restarted its numbering.
    """
    from app.main import trip_slices, tracker_window
    from app.models import FlightLeg
    from datetime import date as _d

    def L(n, day, start=False):
        return FlightLeg(id=n, date=_d(2026, 8, day), flight_number=n,
                         origin="DFW", destination="OKC",
                         dep_time_local="06:00", arr_time_local="07:30",
                         trip_start=start)

    roster = [L("a1", 1, True), L("a2", 2),
              L("b1", 10, True), L("b2", 11), L("b3", 12),
              L("c1", 20, True),
              L("d1", 28, True)]

    trips = trip_slices(roster)
    check("the roster cuts into four trips", len(trips) == 4, str(len(trips)))
    check("...at the trip_start markers",
          [len(t) for t in trips] == [2, 3, 1, 1], str([len(t) for t in trips]))

    # Anchored mid-trip: that whole trip, flown legs and all. Just it.
    w = tracker_window(roster, "b2")
    check("the anchor's whole trip is kept, including what is already flown",
          {"b1", "b2", "b3"} == w, str(w))
    check("...and NOT the next trip", "c1" not in w, str(w))
    check("...nor the one after that", "d1" not in w, str(w))
    check("...and nothing older", "a1" not in w and "a2" not in w, str(w))

    # The last trip on the roster has no successor and must not blow up.
    check("a final trip is handled", tracker_window(roster, "d1") == {"d1"})

    # DEGRADE TO THE OLD BEHAVIOUR, NEVER TO A BLANK PAGE. A roster with
    # no trip markers at all (pasted without the blank lines the parser
    # keys on) is one trip containing everything.
    flat = [L("x1", 1), L("x2", 2), L("x3", 3)]
    check("an unmarked roster is a single trip", len(trip_slices(flat)) == 1)
    check("...so every leg still shows",
          tracker_window(flat, "x2") == {"x1", "x2", "x3"})
    # And an anchor that cannot be placed says "no opinion" rather than
    # returning an empty set, which would render an empty tracker.
    check("an unplaceable anchor shows everything", tracker_window(roster, "zzz") is None)
    check("no anchor at all shows everything", tracker_window(roster, None) is None)


def test_a_finished_trip_holds_the_tracker_for_ten_hours():
    """Landing does not wipe the trip off the page. (1.16.0)

    With the window cut to one trip, the anchor is the only thing left
    deciding which trip that is — so the moment the last leg went past,
    the tracker would have jumped to a trip a fortnight out and someone
    opening the app while he was still in the crew van would have seen no
    sign the flight that just landed ever happened.
    """
    from app.main import tracker_anchor, tracker_window, TRIP_HANDOVER
    from app.models import CurrentFlightInfo

    # Trip A finishes 21 Aug. Trip B starts 4 Sep.
    a1 = leg("a1", date(2026, 8, 21), "AA100", "DFW", "AEX", "08:50", "10:11")
    a2 = leg("a2", date(2026, 8, 21), "AA200", "AEX", "DFW", "18:00", "19:20")
    a1.trip_start = True
    b1 = leg("b1", date(2026, 9, 4), "AA300", "DFW", "OKC", "07:00", "08:00")
    b1.trip_start = True
    roster = [a1, a2, b1]

    landed = a2.arr_datetime_utc()
    info = CurrentFlightInfo(current=None, next=b1, past=[a1, a2],
                             upcoming=[b1], all_legs=roster)

    # An hour after the last landing: still his trip.
    soon = landed + timedelta(hours=1)
    check("an hour after the last landing the finished trip still anchors",
          tracker_anchor(info, soon).id == "a2")
    check("...so the list is still that trip",
          tracker_window(roster, tracker_anchor(info, soon).id) == {"a1", "a2"})

    # Just inside ten hours: still his trip. Just outside: the next one.
    check("just inside the handover it is still the finished trip",
          tracker_anchor(info, landed + TRIP_HANDOVER - timedelta(minutes=1)).id == "a2")
    check("just outside it, the next trip takes over",
          tracker_anchor(info, landed + TRIP_HANDOVER + timedelta(minutes=1)).id == "b1")
    check("...and the list follows it there",
          tracker_window(roster, "b1") == {"b1"})

    # A live leg beats everything.
    live_info = CurrentFlightInfo(current=a2, next=b1, past=[a1],
                                  upcoming=[b1], all_legs=roster)
    check("a live leg outranks the handover", tracker_anchor(live_info, soon).id == "a2")

    # THE TEN HOURS IS FAR 117'S TEN HOURS. Minimum rest between duty
    # periods, so a legal schedule cannot start the next trip inside the
    # window. A cap at the next departure was written and removed: it
    # could only fire on an illegal or mis-imported schedule, and rule 1
    # already covers that, because the leg goes live twenty minutes
    # before it pushes.
    legal = leg("q1", date(2026, 8, 22), "AA400", "DFW", "OKC", "09:00", "10:00")
    legal.trip_start = True
    rest_hours = (legal.dep_datetime_utc() - landed).total_seconds() / 3600
    check("the fixture respects the ten-hour rest minimum", rest_hours >= 10,
          f"{rest_hours:.1f}h")
    rest_info = CurrentFlightInfo(current=None, next=legal, past=[a1, a2],
                                  upcoming=[legal], all_legs=[a1, a2, legal])
    check("the handover expires before the next legal departure",
          tracker_anchor(rest_info, landed + TRIP_HANDOVER + timedelta(minutes=1)).id == "q1")
    check("...and a live leg would win regardless of the clock",
          tracker_anchor(
              CurrentFlightInfo(current=legal, next=None, past=[a1, a2],
                                upcoming=[], all_legs=[a1, a2, legal]),
              soon).id == "q1")

    # An empty schedule must say "no opinion", not raise.
    check("an empty schedule anchors nowhere",
          tracker_anchor(CurrentFlightInfo(), soon) is None)


def test_the_calendar_draws_flights_with_the_shared_strip(uid):
    """The last page still drawing its own flight row. (1.18.0)

    Invariant 25 named three surfaces that had each grown their own
    markup for "flight number, city pair, two times". The tracker card
    and the tracker list were converted in 1.9.0; the calendar agenda was
    not, and kept a bespoke `.agenda-leg` with its own arrow, its own
    times and NO delay state at all — so a leg that went two hours late
    read here exactly like one that ran to the minute.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "calendar.html"), encoding="utf-8") as fh:
        html = fh.read()

    def code_only(text):
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        return re.sub(r"^\s*//.*$", "", text, flags=re.M)

    code = code_only(html)
    check("the calendar draws flights with the shared strip",
          "fstrip fstrip--sm" in code)
    check("...and the bespoke row is gone", ".agenda-leg-main" not in code)
    # Same rule the viewer is held to: USING the class is the point,
    # DECLARING it in a template is the regression. A CONTEXTUAL override
    # — `.cal-leg-head > .fstrip`, the way `.fstrip-head .status` works —
    # is explicitly allowed and stays with the surface that needs it. So
    # the check is for a BARE `.fstrip {` at the head of a selector, not
    # for the string appearing in one.
    bare = re.search(r"(?:^|[,{}])\s*\.fstrip(--\w+)?\s*\{", code, re.M)
    check("...without redeclaring the component", bare is None,
          bare.group(0) if bare else "")
    # The arrows must come from the two shared files even though the
    # script builds its markup at runtime — an arrow character written
    # into the JavaScript would be a third departure glyph.
    check("the panel's glyphs come from the shared partials",
          'partials/arrow_out.html' in code and 'partials/arrow_in.html' in code)

    # Leaflet is DEFERRED here, unlike the tracker where the map is the
    # page. Most visits to a month never open a leg.
    check("leaflet does not block the month view",
          'defer src="/static/vendor/leaflet/leaflet.js' in code)

    # THE ROUTE MUST ACTUALLY PAY FOR THE HISTORY. The strip renders
    # `leg.dep_line.state`, which is None unless the caller passed a bulk
    # time index — so the markup above is decorative until this holds.
    with open(os.path.join(here, "app", "main.py"), encoding="utf-8") as fh:
        src = fh.read()
    route = src[src.index("async def calendar_page"):src.index("template = jinja_env.get_template(\"calendar.html\")")]
    check("the calendar builds a time index", "times_by_leg = time_index(user_id)" in route)
    check("...and hands it to every agenda row", "times_by_leg)" in route)
    check("...in ONE query for the month, not one per leg",
          route.count("time_index(") == 1, str(route.count("time_index(")))

    # END TO END on the data the strip reads: a late leg must produce a
    # late STATE, or the row renders plain however good the markup is.
    from app.flights import replace_schedule, write
    day = date(2026, 5, 14)
    legs = [leg("k1", day, "AA10", "DFW", "OKC", "07:00", "08:10")]
    legs[0].trip_start = True
    replace_schedule(uid, legs)
    write("k1", always={
        "out_actual_api": (legs[0].dep_datetime_utc() + timedelta(minutes=40)).isoformat(),
        "in_actual_api": (legs[0].arr_datetime_utc() + timedelta(minutes=52)).isoformat(),
    })
    now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    row = app_main.leg_view(legs[0], now, "24", app_main.tag_index(uid),
                            app_main.time_index(uid))
    check("a late leg carries a late departure state",
          row["dep_line"] and row["dep_line"]["state"] == "late", str(row["dep_line"]))
    check("...and a late arrival state",
          row["arr_line"] and row["arr_line"]["state"] == "late", str(row["arr_line"]))
    check("...and the time it actually went, not the one it was given",
          row["dep_line"]["was_short"] and
          row["dep_line"]["time_short"] != row["dep_line"]["was_short"],
          str(row["dep_line"]))


def test_the_calendar_row_opens_one_at_a_time(uid):
    """A month can hold sixty legs; it must not hold sixty maps. (1.18.0)"""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "calendar.html"), encoding="utf-8") as fh:
        html = fh.read()

    # The panel ships EMPTY. Rendering every leg's history and map
    # container into the document would make a month enormous to answer a
    # question about one flight.
    check("detail panels ship empty and hidden",
          re.search(r'class="cal-detail"[^>]*hidden[^>]*>\s*</div>', html) is not None)
    check("...and are filled from the existing leg endpoint",
          "/api/v1/leg/" in html)
    # Opening a row must tear the previous map down, or they accumulate.
    check("opening a row closes the one before it", "closeOpen()" in html)
    check("...and removes its map", "openMap.remove()" in html)
    # A thumbnail, not a map you drive: a draggable map inside a
    # scrolling page swallows the page's scroll (1.10.2, 1.12.0).
    for opt in ("dragging: false", "touchZoom: false", "scrollWheelZoom: false"):
        check(f"the mini map sets {opt}", opt in html)
    # A response landing after the user closed the row must not paint it
    # back in.
    check("a late response cannot reopen a closed row",
          html.count("if (openRow !== row) return;") >= 2)

    # The row is a real control, not a div with a handler.
    check("the row is a button", "<button class=\"cal-leg-head\"" in html)
    check("...that announces its state", 'aria-expanded="false"' in html)

    # The endpoint it calls must answer for a PAST leg — the whole point
    # of a history browser — and resolve_selected_leg is what decides
    # that. It matches against info.all_legs, so any leg still inside
    # retention resolves however old it is.
    from app.flights import replace_schedule
    from app.schedule import get_current_info
    old = date(2026, 2, 3)
    legs = [leg("h1", old, "AA77", "DFW", "SGF", "09:00", "10:20")]
    legs[0].trip_start = True
    replace_schedule(uid, legs)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    info = get_current_info(uid, now)
    picked, _ = app_main.resolve_selected_leg(info, "h1", now)
    check("an old leg still resolves months later", picked.id == "h1", str(picked))
    view = app_main.leg_view(picked, now, "24", app_main.tag_index(uid))
    check("...and carries the route facts the panel prints",
          view["route_nm"] is not None and view["block_time"] is not None,
          str((view["route_nm"], view["block_time"])))


def test_named_share_codes_keep_existing_shares_working():
    """N4. Name a code, add another, keep what exists working. (1.23.0,
    reworked 1.24.0.)

    The part that breaks silently is the last clause — a family does not
    report "my code stopped working", they just stop opening the app.
    """
    from datetime import date as _date
    from app.db import get_connection
    from app.auth import (create_user as _mk, get_user_by_share_code,
                          share_codes_for, add_share_code, update_share_code,
                          delete_share_code, regenerate_share_code)

    who = _mk("n4pilot", "pw-not-used")
    original = get_connection().execute(
        "SELECT share_code FROM users WHERE id = ?", (who,)).fetchone()["share_code"]

    # A NEW ACCOUNT'S CODE MUST WORK IMMEDIATELY. create_user writes the
    # invite row in the same transaction as the user; without that a
    # brand-new pilot hands out five digits that log nobody in, and the
    # db.py backfill does not rescue them until the next restart.
    rows = share_codes_for(who)
    check("a new account gets its invite row at once", len(rows) == 1, str(rows))
    check("...carrying the code the pilot is already showing",
          rows[0]["code"] == original, str(rows))
    check("...and it resolves", get_user_by_share_code(original) is not None)

    # ADDING NEVER DISTURBS WHAT EXISTS — "keep current shares intact".
    # The row is created UNNAMED and named in place afterwards.
    sarah = add_share_code(who)
    sid = [r for r in share_codes_for(who) if r["code"] == sarah][0]["id"]
    check("a new share is created without demanding a name first",
          not (share_codes_for(who)[-1]["name"] or ""))
    check("a second invite does not change the first",
          get_user_by_share_code(original) is not None)
    check("...and the new one works too", get_user_by_share_code(sarah) is not None)
    check("...with different digits", sarah != original)

    update_share_code(who, sid, "Sarah", "")
    check("a row can be named in place",
          [r for r in share_codes_for(who) if r["id"] == sid][0]["name"] == "Sarah")

    # EXPIRY.
    today = _date.today()
    update_share_code(who, sid, "Sarah", (today + timedelta(days=10)).isoformat())
    check("a future expiry leaves the code working",
          get_user_by_share_code(sarah) is not None)
    check("...and it is not flagged expired",
          not [r for r in share_codes_for(who) if r["id"] == sid][0]["is_expired"])

    update_share_code(who, sid, "Sarah", (today - timedelta(days=1)).isoformat())
    check("a past expiry stops the code resolving",
          get_user_by_share_code(sarah) is None)
    check("...and the page flags it",
          [r for r in share_codes_for(who) if r["id"] == sid][0]["is_expired"])
    check("...without touching anyone else",
          get_user_by_share_code(original) is not None)

    # THE BOUNDARY. "Expires 24 Aug" plainly means good ON the 24th; the
    # other reading cuts someone off a day early, which they experience as
    # the app being broken.
    update_share_code(who, sid, "Sarah", today.isoformat())
    check("a code expiring TODAY still works today",
          get_user_by_share_code(sarah) is not None)

    # A mangled date FAILS SAFE to "never". Silently meaning "expired"
    # would lock a family out with no error anywhere to see.
    update_share_code(who, sid, "Sarah", "24/08/2026")
    check("an unparseable date is stored as no expiry",
          ([r for r in share_codes_for(who) if r["id"] == sid][0]["expires_at"] or "") == "")
    check("...so the code keeps working rather than dying silently",
          get_user_by_share_code(sarah) is not None)

    # DELETE IS A DELETE (1.24.0). 1.23.0 kept revoked rows listed; a list
    # of dead codes nobody reads is not worth growing under one they do.
    delete_share_code(who, sid)
    check("a deleted code stops resolving", get_user_by_share_code(sarah) is None)
    check("...and leaves the list", not any(r["id"] == sid for r in share_codes_for(who)))
    check("...while everyone else is untouched",
          get_user_by_share_code(original) is not None)

    # Codes must be unique across ALL pilots: two households on one code
    # would each see the other's position feed.
    all_codes = [r["code"] for r in get_connection().execute(
        "SELECT code FROM share_codes")]
    check("every code in the table is unique",
          len(all_codes) == len(set(all_codes)), str(all_codes))

    # THE LEGACY WHOLE-ACCOUNT REGENERATE. Its button is gone but the
    # route is still reachable from a stale page, and auth resolves
    # through share_codes now.
    regenerate_share_code(who)
    now_users = get_connection().execute(
        "SELECT share_code FROM users WHERE id = ?", (who,)).fetchone()["share_code"]
    fam = [r for r in share_codes_for(who) if r["name"] == "Family"][0]
    check("the legacy regenerate keeps both tables in step",
          fam["code"] == now_users, f"{fam['code']} vs {now_users}")
    check("...and the old code no longer resolves",
          get_user_by_share_code(original) is None)


def test_the_accent_is_readable_in_both_of_its_jobs():
    """One colour could not do both jobs. (1.24.2)

    --accent is a LINK colour on a dark navy card and a BUTTON FILL behind
    white text. Those pull in opposite directions: the first wants a light
    value, the second a dark one. The old single blue compromised and lost
    the button — 3.68:1 behind white text, under the 3:1 floor for UI text
    and nowhere near the 4.5:1 for body.

    Computed rather than eyeballed, because "looks fine on my monitor" is
    how contrast bugs ship.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "static", "app.css"), encoding="utf-8") as fh:
        css = fh.read()

    def lum(h):
        h = h.lstrip("#")
        c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        r, g, b = [f(v) for v in c]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def ratio(a, b):
        hi, lo = sorted([lum(a), lum(b)], reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    def var_in(block, name):
        seg = css.split(block, 1)[1].split("}", 1)[0]
        m = re.search(rf"{name}:\s*(#[0-9a-fA-F]{{6}})", seg)
        return m.group(1) if m else None

    check("the accent is split into a link and a fill",
          css.count("--accent-fill:") >= 2, str(css.count("--accent-fill:")))

    dark_link = var_in('[data-theme="dark"] {', "--accent")
    dark_fill = var_in('[data-theme="dark"] {', "--accent-fill")
    light_link = var_in('[data-theme="light"] {', "--accent")
    light_fill = var_in('[data-theme="light"] {', "--accent-fill")
    check("all four values are declared",
          all([dark_link, dark_fill, light_link, light_fill]),
          str([dark_link, dark_fill, light_link, light_fill]))
    if not all([dark_link, dark_fill, light_link, light_fill]):
        return

    # 4.5:1 is the body-text floor, and a link is body text.
    r = ratio(dark_link, "#1a2332")
    check("a dark-mode link clears 4.5:1 on the card", r >= 4.5, f"{r:.2f}")
    r = ratio(light_link, "#ffffff")
    check("a light-mode link clears 4.5:1 on the card", r >= 4.5, f"{r:.2f}")
    # The filled button carries white text.
    r = ratio(dark_fill, "#ffffff")
    check("the dark-mode button fill clears 4.5:1 behind white", r >= 4.5, f"{r:.2f}")
    r = ratio(light_fill, "#ffffff")
    check("the light-mode button fill clears 4.5:1 behind white", r >= 4.5, f"{r:.2f}")

    # It must not be mistaken for a STATUS. Green, red and amber already
    # mean on time, late and caution on the strips.
    for status in ("--good", "--bad", "--warn"):
        val = var_in('[data-theme="dark"] {', status)
        if val:
            check(f"the accent is distinguishable from {status}",
                  abs(lum(dark_link) - lum(val)) > 0.02 or dark_link != val,
                  f"{dark_link} vs {val}")


def test_the_share_table_looks_like_the_flight_table():
    """Edited in place, styled like its neighbour. (1.24.0)"""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "flights.html"), encoding="utf-8") as fh:
        html = fh.read()

    # THE SAME TABLE AS THE ROSTER. Two tables on one page styling
    # themselves differently is most of why this page read as unfinished.
    check("shares are a table", '<table class="share-table">' in html)
    before = html.split('class="share-table"')[0]
    check("...inside the same scroller the roster uses",
          '<div class="table-scroll">' in before[-400:], before[-120:])
    check("...with a real thead", "<th>Name</th>" in html and "<th>Expires</th>" in html)

    # EDIT IN PLACE, no dialog and no name box standing in front of the
    # button. A <form> cannot wrap a <tr>, so rows associate by id.
    check("rows carry editable fields", 'name="expires_at"' in html)
    check("...bound to a per-row form by id", 'form="sh-' in html)
    check("there is no name box gating the add button",
          'placeholder="Name (e.g. Sarah)"' not in html)
    check("the button says what it makes", "New share" in html)

    # ONE BUTTON PER JOB. Copy and New (regenerate) are gone.
    check("each row can be shared", "data-share-send" in html)
    # ICONS, NOT LABELS (1.24.1). The word "Share" sat beside a date
    # input, whose native picker indicator renders right next to it on
    # desktop — the two read as one control with a calendar stuck on.
    check("...with a glyph from the shared partial",
          'partials/share_glyph.html' in html)
    check("...not the word Share in a box", ">Share</button>" not in html)
    # The roster below deletes a row with a bare X. Two tables on one page
    # must not disagree about what deleting looks like.
    check("delete uses the same control as the roster's",
          'class="delete-btn"' in html.split('data-share-send', 1)[1][:400])

    # MOBILE: four columns, two of them inputs and one a native date
    # picker, do not fit across a phone — and the date input is the widest
    # thing on the row, which is why it scrunched as soon as a date was
    # set. Under 600px the rows become blocks.
    mq = html.split("@media (max-width: 600px)", 1)
    check("the share table restacks on a phone", len(mq) == 2)
    if len(mq) == 2:
        block = mq[1][:1400]
        check("...rows become blocks", ".share-table tr" in block and "display: block" in block)
        check("...and the header is dropped", ".share-table thead" in block)
        check("...with no sideways scroll left to trap the focus ring",
              "overflow-x: visible" in block)

    # THE BUTTON WAS THE PROBLEM, NOT THE HUE. A solid fill of --accent is
    # the default primary every framework ships; a large block of it is
    # what reads as cheap. One filled button per page, and this is not it.
    check("New share is quiet, not a slab of accent",
          "btn-quiet" in html and "btn-new-share" not in html)
    quiet = html.split(".btn-quiet {", 1)[1].split("}", 1)[0]
    check("...so it does not fill with the accent colour",
          "background: var(--accent)" not in quiet, quiet)
    check("the copy button is gone", "data-share-copy" not in html)
    check("the per-row regenerate is gone", "/flights/shares/regenerate" not in html)
    check("...and so is revoke", "/flights/shares/revoke" not in html)
    check("delete is the only removal", "/flights/shares/delete" in html)
    check("...and it asks first", "cannot be undone" in html)

    # Revoked rows no longer linger.
    check("nothing is kept around as revoked", "is-revoked" not in html)

    # Autosave: `change` covers a blurred text field AND a picked date.
    check("edits save without a Save button", "addEventListener('change'" in html)
    check("...and Enter commits rather than reloading",
          "ev.preventDefault()" in html.split("keydown", 1)[1][:400])
    check("...and a row cannot submit twice", "pending.has" in html)
    # A cancelled share sheet is a choice, not a failure.
    check("backing out of the share sheet copies nothing", "AbortError" in html)


def test_late_is_measured_from_the_airlines_own_schedule(uid):
    """The FFDO is not a schedule once the leg is flown. (1.21.0)

    The owner found this in real use: the FFDO RESTATES a flown leg at
    the time it actually went. So a leg pushed at 12:35 against a 12:29
    line comes back from a post-flight paste reading 12:35 — and if it
    was not already on the roster when it flew, 12:35 goes in as its
    scheduled time and the six minutes are gone for good. It reads on
    time forever. Legs added mid-trip and imported afterwards hit this
    every time.

    `out_scheduled` is immune: enrichment.py writes it ONCE, the first
    time AeroAPI sees the flight, and a delay moves `out_estimated` and
    `out_actual_api` instead. It has been stored since 1.4.0 and nothing
    ever read it back.
    """
    from app.flights import merge_schedule, write, get_flight
    from app.view import strip_lines, build
    from app import tags

    d = date(2026, 7, 10)
    # The paste already carries the ACTUAL time, which is the bug.
    l = leg("sb1", d, "AA2673", "DFW", "OKC", "12:35", "13:40")
    l.trip_start = True
    merge_schedule(uid, [l])
    sched = datetime(2026, 7, 10, 17, 29, tzinfo=timezone.utc)   # true 12:29
    actual = datetime(2026, 7, 10, 17, 35, tzinfo=timezone.utc)  # went 12:35

    # WITHOUT the API, nothing is invented: it falls back to the FFDO and
    # says "scheduled", not "on time". Unreported is not on time.
    dep, _ = strip_lines(l, get_flight("sb1"), tags.PHASE_ARRIVED, False, True, "24")
    check("with no airline data it falls back to the pasted time",
          dep["time_short"] == "12:35" and dep["state"] == "scheduled", str(dep))

    write("sb1", always={"out_actual_api": actual.isoformat(),
                         "out_scheduled": sched.isoformat()})
    dep, _ = strip_lines(l, get_flight("sb1"), tags.PHASE_ARRIVED, False, True, "24")
    check("the airline's own schedule recovers the lost six minutes",
          dep["state"] == "late" and dep["minutes"] == 6, str(dep))
    check("...and the row shows what it displaced",
          dep["was_short"] == "12:29", str(dep))

    # A DELAY MUST NOT MOVE THE BASELINE — the owner's question. It cannot:
    # out_scheduled is in enrichment's `once` block, delays are `latest`.
    write("sb1", always={"out_estimated": (sched + timedelta(minutes=90)).isoformat(),
                         "out_actual_api": (sched + timedelta(minutes=90)).isoformat()})
    row = get_flight("sb1")
    check("a delay does not touch out_scheduled",
          row["out_scheduled"] == sched.isoformat(), str(row["out_scheduled"]))
    dep, _ = strip_lines(l, row, tags.PHASE_ARRIVED, False, True, "24")
    check("...so a 90 minute delay reads as 90 minutes, not as a new schedule",
          dep["minutes"] == 90 and dep["was_short"] == "12:29", str(dep))

    # ONE BASELINE, TWO SURFACES. build() kept its own copy of this pair,
    # so the card and the list could measure "late" from different times —
    # the split strip_lines was extracted to close.
    v = build(row, l, l.arr_datetime_utc(), "24")
    check("the card agrees with the list",
          v["dep_line"]["minutes"] == dep["minutes"] and
          v["dep_line"]["was_short"] == dep["was_short"], str(v["dep_line"]))


def test_flown_legs_are_removed_by_hand_never_by_default():
    """Remove all, individual X, nothing pre-selected. (1.21.0)"""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "import_review.html"), encoding="utf-8") as fh:
        html = fh.read()

    # Sliced to the section's own closing tag, not to the first
    # {% endif %} — that one belongs to the inline deadhead conditional
    # INSIDE the first row, so the slice stopped before the X button and
    # three assertions passed on an empty-ish string.
    sec = html.split('class="diff-sec sec-flown"', 1)[1].split("</div>\n    {% endif %}", 1)[0]
    # NOTHING PRE-SELECTED. This is the safety mechanism, not a style
    # choice: pasting one trip says nothing about the rest of the month,
    # so a pre-ticked list would delete a month of logbook by default.
    check("no flown leg is pre-selected for removal", "checked" not in sec, sec[:200])
    check("...while an upcoming leg still is",
          "checked" in html.split('class="diff-sec sec-removed"', 1)[1][:900])
    check("the section asks whether they were flown", "Did you fly these?" in sec)
    check("there is a remove-all", "data-flown-all" in sec)
    check("...and a way back", "data-flown-none" in sec)
    check("each flight has its own X", "data-flown-x" in sec)

    # ONE PIECE OF FORM STATE. The X drives the checkbox rather than
    # replacing it, so there is no second removal mechanism to keep in
    # step with the first.
    check("the X drives the real checkbox", "data-flown-box" in sec)
    check("...which is still the submitted control", 'name="remove_id"' in sec)
    # Visually hidden, NOT display:none, which would take it out of the
    # tab order and off the accessibility tree and leave the X — a button
    # with no state — as the only way in.
    css = html.split(".flown-check {", 1)[1].split("}", 1)[0]
    check("the checkbox stays reachable by keyboard", "display: none" not in css, css)

    # ITS OWN LISTENER ON THE DOCUMENT. The first version of this put the
    # branch inside the break-list handler, where a click on a flown row
    # never reaches it.
    check("the handler is not trapped in the break-list listener",
          "breakList.addEventListener" not in
          html.split("FLOWN-LEG REMOVAL", 1)[1].split("})();", 1)[0])
    check("a change on the checkbox itself still updates the row",
          "addEventListener('change'" in html)


def test_the_mini_map_says_whether_it_knows_the_path(uid):
    """Solid means observed. Dashed means guessed. (1.20.0)

    The mini map drew a straight solid line between the two airports in
    every case. On a leg with a recorded track that threw the track away;
    on a leg without one it stated a flight path the app never observed,
    quietly turning "nothing was tracked" into "flew direct". Storing
    positions for a year is only worth the rows if something reads them
    back, and this is that something.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "calendar.html"), encoding="utf-8") as fh:
        html = fh.read()

    check("the map reads the leg's recorded track", "data-track" in html)
    check("...and dashes the line when there is none", "dashArray" in html)
    # The dash must belong to the no-track branch. A dashArray applied to
    # both would make the distinction decorative.
    branch = html.split("if (track.length >= 2) {", 1)[1]
    solid, dashed = branch.split("} else {", 1)
    dashed = dashed.split("fitTo = [a, b];", 1)[0]
    check("...the tracked path is NOT dashed", "dashArray" not in solid, solid[:160])
    check("...and the untracked one is", "dashArray" in dashed, dashed[:160])
    # Anchored to the airports, so a track that starts late still reads as
    # a journey between two fields rather than a line in open country.
    check("the flown path is anchored to both airports",
          "[a].concat(track, [b])" in html)

    # The endpoint the panel reads must actually carry the track for an
    # OLD leg — the live payload hands back an empty breadcrumb for one
    # that finished months ago, which is exactly what the calendar asks
    # about.
    from app.flights import merge_schedule
    from app.track import record_position, get_breadcrumb
    from app.schedule import get_current_info
    day = date(2026, 4, 2)
    legs = [leg("mm1", day, "AA55", "DFW", "OKC", "08:00", "09:00")]
    legs[0].trip_start = True
    merge_schedule(uid, legs)
    base = datetime(2026, 4, 2, 13, 0, tzinfo=timezone.utc)
    for i, (la, lo) in enumerate([(32.9, -97.0), (34.0, -97.4), (35.4, -97.6)]):
        record_position("mm1", la, lo, base + timedelta(minutes=i * 10))
    check("positions are recorded against the leg", len(get_breadcrumb("mm1")) == 3)
    check("...and survive to be read back months later",
          get_breadcrumb("mm1")[0] == [32.9, -97.0], str(get_breadcrumb("mm1")[:1]))


def test_an_open_calendar_row_stops_repeating_itself():
    """The strip's times give way to the panel's. (1.20.0)

    On the tracker this never arises: the detail panel covers the list.
    On the calendar the row stays on screen above its own expansion, so
    the same two times were printed twice, three lines apart — once small
    on the strip and once properly in .aptblock with what they displaced.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "calendar.html"), encoding="utf-8") as fh:
        html = fh.read()
    check("an open row hides the strip's times",
          ".cal-leg.open .fstrip-ends { display: none; }" in html)
    # The flight number and pills must NOT be hidden: they are the row's
    # identity and are not restated in the panel below.
    check("...but not the row's identity",
          ".cal-leg.open .fstrip-head" not in html)

    # The provenance row needs its own layout here. .detail-row is
    # declared inside viewer.html's <style>, so on this page the key and
    # value had none and printed as "Arrival time fromAirline".
    check("the calendar lays out its own provenance row",
          ".cal-detail .detail-row {" in html)
    check("...as two columns, not one run-on string",
          "justify-content: space-between" in
          html.split(".cal-detail .detail-row {", 1)[1].split("}", 1)[0])


def test_a_closed_leg_settles_out_after_thirty_minutes():
    """A flown leg leaves the list half an hour after it closes. (1.17.0)

    Pass 3b. The list is about the trip you are ON; a four-leg day should
    not end as four rows about the past and one about the present.
    """
    from app.main import settled_out, LEG_SETTLE

    now = datetime(2026, 8, 21, 18, 0, tzinfo=timezone.utc)

    def row(closed, closed_at=None):
        return {"closed": 1 if closed else 0,
                "closed_at": closed_at.isoformat() if closed_at else None}

    # dict.keys() is what the real sqlite3.Row exposes, and settled_out
    # probes with `in row.keys()` — so the fixture must answer the same
    # way or the test proves nothing about the real path.
    long_ago = now - LEG_SETTLE - timedelta(minutes=5)
    just_now = now - timedelta(minutes=5)

    times = {
        "a": row(True, long_ago),    # closed and settled -> goes
        "b": row(True, just_now),    # closed, still settling -> stays
        "c": row(False),             # not closed -> stays
        "d": row(True, None),        # closed with NO timestamp -> stays
    }
    drop = settled_out(["a", "b", "c", "d"], times, now)
    check("a leg closed over thirty minutes ago leaves", "a" in drop, str(drop))
    check("...one closed five minutes ago stays", "b" not in drop, str(drop))
    check("...an open leg stays however old", "c" not in drop, str(drop))
    check("...and a closeout with no timestamp never drops", "d" not in drop, str(drop))

    # THE BOUNDARY. Exactly thirty minutes drops; a second under does not.
    # TWO legs, not one: with a single leg the never-empty guard refuses
    # the drop and the fixture would be testing that guard instead of the
    # threshold it names. `keep` is a live leg, so it never drops itself.
    def at(delta):
        return settled_out(["x", "keep"],
                           {"x": row(True, now - delta), "keep": row(False)}, now)
    check("the rule fires exactly at the threshold", at(LEG_SETTLE) == {"x"})
    check("...and not a second before",
          at(LEG_SETTLE - timedelta(seconds=1)) == set())

    # THE ONE THAT MATTERS. If every leg has settled, the list would empty
    # itself and STAY empty for the rest of the ten-hour handover — the
    # exact window in which someone opens the app to check he got in.
    all_done = {"p": row(True, long_ago), "q": row(True, long_ago)}
    drop = settled_out(["p", "q"], all_done, now)
    check("the last leg of a finished trip never drops", "q" not in drop, str(drop))
    check("...but the ones before it still do", "p" in drop, str(drop))

    # Degrade safely: no times at all means drop nothing, never everything.
    check("a missing time index drops nothing", settled_out(["a"], None, now) == set())
    check("...and so does an empty one", settled_out(["a"], {}, now) == set())


def test_the_settled_leg_rule_cannot_empty_the_tracker(uid):
    """End to end: a fully-flown trip still renders. (1.17.0)

    settled_out is unit-tested above; this checks the rule as the page
    actually applies it, because the guard depends on the filter running
    AFTER the trip window rather than before.
    """
    from app.main import build_flight_list, _assign_trip_day_numbers
    from app.schedule import get_current_info
    from app.flights import replace_schedule, write

    day = date(2026, 3, 10)
    legs = [leg("s1", day, "AA1", "DFW", "OKC", "08:00", "09:00"),
            leg("s2", day, "AA2", "OKC", "DFW", "11:00", "12:00")]
    legs[0].trip_start = True
    replace_schedule(uid, legs)

    # Both flown and closed well over the settle window.
    closed_at = datetime(2026, 3, 10, 18, 0, tzinfo=timezone.utc)
    for lid in ("s1", "s2"):
        write(lid, always={"closed": 1, "closed_at": closed_at.isoformat()})

    now = closed_at + timedelta(hours=2)
    info = get_current_info(uid, now)
    groups = build_flight_list(info, _assign_trip_day_numbers(info.all_legs), now,
                               "24", app_main.tag_index(uid), {},
                               app_main.time_index(uid))
    shown = [r["id"] for g in groups for r in g["legs"]]
    check("a wholly-settled trip still shows its last leg", shown == ["s2"], str(shown))
    check("...and the tracker is never empty", len(shown) >= 1, str(shown))


def test_the_card_and_the_list_agree_on_the_trip():
    """One anchor, two consumers. (1.16.0)

    These used to compute their default from the same three fallbacks
    separately. That agreed right up until the handover was added on one
    side, at which point the card could be showing the first leg of a
    trip the list does not contain.
    """
    from app.main import resolve_selected_leg, tracker_anchor
    from app.models import CurrentFlightInfo

    a1 = leg("a1", date(2026, 8, 21), "AA100", "DFW", "AEX", "08:50", "10:11")
    a1.trip_start = True
    b1 = leg("b1", date(2026, 9, 4), "AA300", "DFW", "OKC", "07:00", "08:00")
    b1.trip_start = True
    info = CurrentFlightInfo(current=None, next=b1, past=[a1],
                             upcoming=[b1], all_legs=[a1, b1])
    just_landed = a1.arr_datetime_utc() + timedelta(hours=2)

    selected, _ = resolve_selected_leg(info, None, just_landed)
    check("the card defaults to the same leg the list is anchored on",
          selected.id == tracker_anchor(info, just_landed).id == "a1")

    # An explicit tap still wins, as it always did.
    tapped, _ = resolve_selected_leg(info, "b1", just_landed)
    check("...but tapping a flight still selects that flight", tapped.id == "b1")


def test_list_rows_carry_delay_state():
    """A row and the card above it cannot disagree about lateness. (1.11.0)

    List rows printed a bare scheduled time with no state at all, so a
    flight could read plain in the list and red on the card in the same
    breath. Both now reach the same two dicts through the same _variance
    and _time_line.
    """
    from app.view import strip_lines
    from app.airports import enrich_leg
    from app.models import FlightLeg
    from datetime import date as _d, timedelta as _td

    l = FlightLeg(id="S1", date=_d(2026, 8, 16), flight_number="3729",
                  origin="DFW", destination="OKC",
                  dep_time_local="06:00", arr_time_local="07:22")
    enrich_leg(l)

    # No flights row at all: an unflown leg. Scheduled, and NOT green.
    dep, arr = strip_lines(l, None, "Scheduled", False, False, "24")
    check("an unflown leg still shows its times", dep and arr)
    check("...tagged scheduled, not on time",
          dep["state"] == "scheduled" and arr["state"] == "scheduled")

    late = (l.arr_datetime_utc() + _td(minutes=18)).isoformat()
    row = {"out_actual_api": None, "out_observed": None, "out_estimated": None,
           "in_actual_api": None, "in_observed": None, "in_estimated": late}
    dep, arr = strip_lines(l, row, "In air", False, False, "24")
    check("an 18-minute delay reads late", arr["state"] == "late", str(arr))
    check("...and carries the corrected time bare", arr["time_short"] is not None)
    check("...and the original to strike through", arr["was_short"] is not None)
    check("...and its zone separately", arr["zone"] == "CT", str(arr["zone"]))

    # A cancelled leg overrides whatever the times say.
    dep, arr = strip_lines(l, row, "In air", True, False, "24")
    check("cancelled wins over the estimate", arr["state"] == "cancelled", str(arr))


def test_strip_ends_cannot_overlap():
    """The zone printed under the next disc. (1.12.1)

    The ends row shipped as a flex row whose items carried `min-width: 0`,
    which lets a flex item shrink BELOW its own content. The times are
    `nowrap`, so rather than shrinking they spilled out of their box and
    the departure's zone superscript landed under the arrival's disc.

    Not a marginal case: 12-hour time adds " PM" to both ends and about a
    third more width, so the format most likely to be in use was the one
    guaranteed to collide. Twelve-hour was never checked when the strip was
    designed.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "static", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    css_code = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    ends = css_code.split(".fstrip-ends {", 1)[1].split("}", 1)[0]
    end = css_code.split(".fstrip-end {", 1)[1].split("}", 1)[0]
    check("an end is never shrunk below its content", "flex: 0 0 auto" in end, end)
    check("...and min-width:0 is not reintroduced to mask an overflow",
          "min-width: 0" not in end, end)
    check("the row wraps rather than overlapping", "flex-wrap: wrap" in ends, ends)
    check("...with a row-gap for the wrapped line",
          re.search(r"gap:\s*[\d.]+rem\s+[\d.]+rem", ends) is not None, ends)


def test_gate_only_no_terminal_or_baggage():
    """Gate stays; terminal line and baggage badge go. (1.12.1, owner's)

    A gate number already tells anyone using it which terminal they want,
    so the terminal line spent a row of every flight restating its
    neighbour. The belt is useful rarely and on screen always.

    DISPLAY decision, not a data one: both are still fetched, still stored,
    still in the payload. Nothing about enrichment changed, so nothing has
    to be re-fetched if they ever come back.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    with open(os.path.join(here, "static", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    code = re.sub(r"^\s*//.*$", "", html, flags=re.M)
    code = re.sub(r"\{#.*?#\}", "", code, flags=re.S)

    check("the gate badge stays", 'id="v-dep-gate"' in code and 'id="v-arr-gate"' in code)
    check("the terminal line is gone from the markup", "aptblock-term" not in code)
    check("...and from the poller", "v-dep-term-text" not in code)
    check("...and its CSS is deleted, not orphaned",
          ".aptblock-term {" not in re.sub(r"/\*.*?\*/", "", css, flags=re.S))
    check("the baggage badge is gone", "v-arr-bag" not in code)

    # The DATA must survive. If enrichment stopped storing these, bringing
    # them back would mean re-querying the airline for flights already paid
    # for — the exact cost this app is built to avoid.
    from app.db import FLIGHT_COLUMNS
    names = [c[0] for c in FLIGHT_COLUMNS]
    check("baggage is still stored", "baggage_claim" in names)
    check("terminals are still stored",
          "terminal_origin" in names and "terminal_destination" in names)


def test_row_tap_selects_the_leg_it_names():
    """1.13.0 opened the panel WITHOUT selecting. (1.14.0)

    So tapping any row showed whichever leg was already selected — usually
    the live one. The worst kind of wrong: nothing blank, nothing thrown,
    the numbers simply somebody else's. Shipped because the panel and the
    selection were wired in different releases and nothing asserted they
    met.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()

    handler = html.split("sheet.addEventListener('click'", 1)[1].split("});", 1)[0]
    check("the tap reads the row's own leg id",
          "row.getAttribute('data-detail-for')" in handler, handler[:200])
    check("...and selects it before opening",
          handler.index("_ptSelectLeg") < handler.index("openPanel()"), handler[:200])
    check("selectLeg is reachable from outside its closure",
          "window._ptSelectLeg = function(id)" in html)
    # ONE selection path. Two would be two ways to select the wrong leg.
    check("the tap and Show-on-map share it",
          html.count("function selectLeg(legId, opts)") == 1)
    # selectLeg also redraws the map, so this is what makes "tapping a
    # flight shows it on the map" true without a second mechanism.
    check("selecting repaints the map",
          "renderMap(currentData, liveData, true)" in html)


def test_only_the_selected_leg_is_drawn():
    """The whole-trip outline is removed. (1.14.1, owner's call)

    Added in 1.14.0, one release earlier. With a row tap now selecting a
    leg and redrawing the map for it, the outline meant every OTHER leg
    stayed drawn underneath the one being looked at — so the answer to
    "where is he" competed with four faint lines that answered nothing.

    Recorded rather than quietly reverted: it was a reasonable idea that
    was wrong once tapping worked, and the pairing is the lesson.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    code = re.sub(r"^\s*//.*$", "", html, flags=re.M)
    check("no trip outline is drawn", "tripRoutes" not in code)
    check("the map draws the selected leg", "function renderMap(cur, live, doFit)" in html)


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


def test_review_page_carries_removals_and_breaks():
    """One page for every decision about a paste. (N1, 1.5.0)

    The owner's instruction was that removals belong on the page that lets
    you add trip separations, not a separate step. Two different removals
    live here and they are NOT the same thing:

      * dropping a leg OUT OF THE PASTE — it was in the FFDO but should not
        be imported;
      * removing a leg already on the ROSTER that this paste no longer
        mentions.

    Both are proposals. Nothing on this page writes anything until confirm.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "import_review.html"),
              encoding="utf-8") as fh:
        ir = fh.read()
    check("legs in the paste can be dropped individually", "drop-leg-btn" in ir)
    check("...on the same page as the trip breaks, not a separate step",
          "drop-leg-btn" in ir and "add-break-btn" in ir)
    check("...by disabling inputs, so the browser simply never posts them",
          "i.disabled = off" in ir)
    check("...leaving the row visible so the choice is reversible",
          ".leg-item.dropped" in ir and "classList.toggle('dropped')" in ir)
    check("a dropped leg does not consume a trip_start slot",
          "classList.contains('dropped')) { return; }" in ir)
    check("roster removals are proposed separately from the paste list",
          'name="remove_id"' in ir and 'name="removable_id"' in ir)
    check("...ticked by default, because a re-paste usually is the truth",
          'name="remove_id"' in ir and "checked" in ir)
    check("the page says which months it is allowed to touch",
          "scope_label" in ir)
    check("...and says outright that flown legs are safe, whether or not",
          "already flown are never removed by an import" in ir)
    check("...that reassurance showing even when nothing is being removed",
          ir.find("already flown are never removed") < ir.find("{% if removed %}"))


def test_flights_page_filters_by_month():
    """N1 made the roster accumulate; this stops it becoming a scroll."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "flights.html"), encoding="utf-8") as fh:
        ah = fh.read()
    with open(os.path.join(here, "app", "main.py"), encoding="utf-8") as fh:
        src = fh.read()
    check("a month filter exists", 'name="month"' in ah and "month-filter" in ah)
    check("...with an all-months escape hatch", "All months" in ah)
    check("...and a count beside each month", "m.count" in ah)
    check("...submitting on change, so it is one tap", "this.form.submit()" in ah)
    check("...but still working with no JavaScript", "<noscript>" in ah)
    check("it is a GET, so it survives a refresh and can be linked",
          'method="get" action="/flights"' in ah)
    check("the select is labelled for screen readers", 'for="month-select"' in ah)
    check("filtering happens on the SERVER, not by hiding rows",
          'l.date.strftime("%Y-%m") == active_month' in src)
    check("an unknown month falls back to everything, never an empty page",
          "month if month in months else None" in src)
    # 1.7.0: the hand-add form is GONE. The owner never asked for it; it
    # was inferred from N1's spec line about a diversion that continued on,
    # and inventing UI from an inference is how a page fills with things
    # nobody wanted. The parser and a re-paste already cover the real case.
    check("there is no hand-add form", 'action="/admin/add"' not in ah
          and "add-grid" not in ah)


def test_calendar_shows_one_month():
    """The calendar used to render EVERY month with data, stacked.

    That was survivable at 30-day retention with a replacing import. With
    365-day retention and N1's additive import it is a year of grids in
    one document, on a phone.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "calendar.html"), encoding="utf-8") as fh:
        ch = fh.read()
    with open(os.path.join(here, "app", "main.py"), encoding="utf-8") as fh:
        src = fh.read()
    check("month navigation exists", "month-nav" in ch)
    check("...with previous and next steps",
          'rel="prev"' in ch and 'rel="next"' in ch)
    check("...as plain links, so browsing needs no script",
          "/calendar?month={{ prev_month }}" in ch)
    check("...disabled rather than absent at either end",
          "month-step disabled" in ch)
    check("a picker allows jumping across a bid cycle",
          "month_choices" in ch and 'id="cal-month"' in ch)
    check("...and works without JavaScript", "<noscript>" in ch)
    check("tap targets are at least 44px", "width: 44px; height: 44px" in ch)
    check("only the viewed month is built",
          "for year, month in [(int(active[:4]), int(active[5:7]))]" in src)
    check("...defaulting to the month actually being lived in",
          "this_month = _key(today.year, today.month)" in src)
    check("...never landing on an empty month by sort order",
          "future[0] if future else available[-1]" in src)
    check("the month is in the URL, so Back steps through months",
          'month: Optional[str] = None' in src)


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
    test_detail_panel_replaces_the_hero_card()
    test_viewer_theme_is_consistent_across_pages()
    test_detail_panels_slide_rather_than_snap()
    test_schedule_works_without_the_map()
    test_session_key_survives_redeploy()
    test_scheduled_time_line_is_marked_as_an_echo()
    test_flight_sheet()
    test_tapping_a_row_is_the_only_way_in()
    test_settings_budget_saves()
    test_no_hardcoded_palette_colours()
    test_flight_strip_is_one_component()
    test_time_line_splits_the_zone_off()
    test_leg_switch_keeps_the_time_rows()
    test_expanded_view_is_per_airport()
    test_route_facts_are_not_measurements()
    test_arrival_source_is_in_english()
    test_live_box_does_not_swallow_the_flight_detail()
    test_list_dropdown_follows_the_same_decisions()
    test_map_cannot_steal_the_scroll()
    test_refit_glides_rather_than_snapping()
    test_fold_and_refit_machinery_is_gone()
    test_tracker_is_scoped_to_one_trip()
    test_a_finished_trip_holds_the_tracker_for_ten_hours()
    test_the_calendar_draws_flights_with_the_shared_strip(create_user("caltest", "pw-not-used"))
    test_the_calendar_row_opens_one_at_a_time(create_user("caltest2", "pw-not-used"))
    test_named_share_codes_keep_existing_shares_working()
    test_the_accent_is_readable_in_both_of_its_jobs()
    test_the_share_table_looks_like_the_flight_table()
    test_late_is_measured_from_the_airlines_own_schedule(create_user("sbtest", "pw-not-used"))
    test_flown_legs_are_removed_by_hand_never_by_default()
    test_the_mini_map_says_whether_it_knows_the_path(create_user("mmtest", "pw-not-used"))
    test_an_open_calendar_row_stops_repeating_itself()
    test_a_closed_leg_settles_out_after_thirty_minutes()
    test_the_settled_leg_rule_cannot_empty_the_tracker(create_user("settletest", "pw-not-used"))
    test_the_card_and_the_list_agree_on_the_trip()
    test_list_rows_carry_delay_state()
    test_strip_ends_cannot_overlap()
    test_gate_only_no_terminal_or_baggage()
    test_row_tap_selects_the_leg_it_names()
    test_only_the_selected_leg_is_drawn()
    test_map_remeasures()
    test_html_is_never_cached()
    test_overnight()
    test_placeholder_purge()
    test_untracked_phase(uid)
    test_sequencing(uid)
    test_flight_list(create_user("listtest", "pw-not-used"))
    test_past_detail_available(create_user("detailtest", "pw-not-used"))
    test_time_lines()
    test_review_page_carries_removals_and_breaks()
    test_flights_page_filters_by_month()
    test_calendar_shows_one_month()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
