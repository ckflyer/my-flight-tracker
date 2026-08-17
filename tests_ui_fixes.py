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


def test_open_card_cannot_outgrow_the_screen():
    """The Show/Hide row must stay reachable. An open card taller than the
    room above the tab pills used to clamp to 0, get held at ~105px by the
    title bar and safe area, and bury its own button under the menu."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()

    check("the old zero clamp is gone",
          "desiredTop = Math.max(0, desiredTop);" not in html)
    check("...replaced by the card's real minimum position",
          "desiredTop = Math.max(minTop(), desiredTop);" in html)
    # minTop must not depend on finding a .topbar: the fallback fired, and
    # assumed ~100px more headroom than the card actually has.
    check("minTop is derived from the card, not a topbar lookup",
          "document.querySelector('.topbar')" not in html)
    check("the panel is capped to the space available", "_ptCapPanel" in html)
    check("...and scrolls inside itself once capped",
          ".expand-details.open.capped {" in html and "overflow-y: auto;" in html)

    # The card grows on its own when live data lands; nothing fired then.
    check("card height changes trigger a relayout", "ResizeObserver" in html)


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

    # Every page a viewer can reach must go through the one helper.
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "app", "main.py"), encoding="utf-8") as fh:
        src = fh.read()
    cal = src[src.index("calendar.html"):]
    cal = cal[:cal.index(")))") + 3] if ")))" in cal[:800] else cal[:800]
    check("the calendar applies viewer overrides",
          "viewer_display_overrides" in cal)


def test_expanding_does_not_disturb_map_or_schedule():
    """Opening the details panel must not re-frame the map, and must never
    make the schedule unreachable."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()

    # layout() branches on this class, and slidePanel calls layout(). Setting
    # the class afterwards ran the open as if the card were still shut, which
    # re-fitted the route into the sliver of map above an open card.
    body = html[html.index("function setExpanded("):]
    body = body[:body.index("function toggle(")]
    check("the expanded class is set before the slide",
          body.index("classList.toggle('expanded'") < body.index("slidePanel("),
          "slidePanel runs first, so layout() sees the old state")

    # The freeze pinned the schedule to invisible while the panel was open.
    # It guarded against a scroll that opening no longer performs, and was the
    # only path that could hide the schedule with no way back.
    check("the scroll fade is not frozen while the panel is open",
          "reveal.style.opacity = '0';\n          reveal.style.pointerEvents = 'none';"
          not in html)
    check("...so scroll position alone drives it",
          "const p = Math.min(1, Math.max(0, (window.scrollY || 0) / RANGE));" in html)


def test_card_grows_upward_from_a_fixed_bottom_edge():
    """Opening the details must not scroll the page. The card's bottom edge
    stays parked above the tab pills and the card grows upward, so the
    Show/Hide row never moves under the reader's thumb."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()

    check("opening no longer scrolls the page to the card",
          "_ptScrollToCard" not in html)
    check("...and closing no longer scrolls it back",
          "_ptScrolledOpen" not in html)
    check("the spacer animates on the same curve as the panel",
          ".hero-space.animating { transition: height 260ms cubic-bezier(0.4, 0, 0.2, 1); }" in html
          and ".expand-details.animating { transition: height 260ms cubic-bezier(0.4, 0, 0.2, 1); }" in html)
    check("layout accepts the card's pending height",
          "function layout(growTo, animate)" in html)
    # The point is that layout() is handed a PREDICTED height rather than
    # re-measuring a card that is still animating. The exact arithmetic
    # gained a term in 1.10.1 (the folding times row), so match the shape,
    # not the literal — a test that pins the expression forces every future
    # correction to look like a regression.
    check("...and the panel hands it a predicted height, not a re-measure",
          re.search(r"_ptLayoutHero\(cardBase[^)]*\+ target, true\)", html)
          is not None)

    # The tab bar is BELOW this script in the document, so querying for it at
    # parse time returns null and every measurement silently falls back to a
    # hardcoded guess. It has to be looked up on demand.
    check("the tab bar is looked up lazily, not at parse time",
          "if (!tabbar) tabbar = document.querySelector('.tabbar');" in html)
    # 1.7.0: the nav is a shared include now, so look for the include
    # rather than the markup. The ORDER is the thing being asserted, and
    # that is unchanged.
    check("...because the nav really does come after the script",
          html.index("<script>") < html.index('partials/tabbar.html'))


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
    check("the card's panel is released back to auto",
          "details.style.height = '';" in html)
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
    check("the row-detail click handler is outside the map block too",
          html.index(".leg-head[data-detail-for]") < bail)
    check("...and the map block still owns show-on-map",
          html.index("data-show-on-map") > 0 and "selectLeg(" in html)
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
    check("no template redeclares the strip or its sizes",
          not re.search(r"\.fstrip(--\w+)?\s*\{", html_code))
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
    for tid, did in (("card-dep-time", "card-dep-disc"),
                     ("card-arr-time", "card-arr-disc")):
        check(f"{did} carries the same state as {tid}",
              f'id="{did}"' in html_code
              and html_code.count("current.dep_line.state") +
                  html_code.count("current.arr_line.state") >= 4)
    # One function writes BOTH halves, so they cannot drift apart.
    check("the poller repaints disc and time together",
          "applyStripTime('card-dep-time', 'card-dep-disc'" in html_code
          and "disc.className = 'fstrip-disc' + state" in html_code)

    # The collapsed strip shows the CORRECTED time and nothing else. The
    # struck-through original and the "12 min late" note belong in the
    # expanded view, where there is room to say it in words.
    check("no delay chip survives on the strip", 'class="chip-delay' not in html_code)
    check("no struck-through original on the strip",
          'id="card-dep-time"' in html
          and "was" not in html.split('id="card-dep-time"')[1].split("</span>")[0])

    # Superscript zones, the 1.3.0 rule, now reachable because the payload
    # emits the zone separately from the time.
    for tid in ("card-dep-time", "card-arr-time"):
        seg = html.split(f'id="{tid}"', 1)[1][:400]
        check(f"{tid} carries its zone as a superscript element",
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
    check("airline reads as the airline",
          ARRIVAL_SOURCE_TEXT["airline"] == "the airline")
    check("observed reads as our own tracking",
          ARRIVAL_SOURCE_TEXT["observed"] == "our own tracking")
    check("estimated is admitted as an estimate",
          ARRIVAL_SOURCE_TEXT["estimated"] == "an estimate")
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
    body = html.split('<div class="expand-details"', 1)[1]
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


def test_strip_times_fold_when_the_panel_opens():
    """The panel says the same two times better, so the summary folds away.

    And the card's spacer maths has to KNOW it folds, or the welded bottom
    edge drifts by exactly the row's height across the 260ms slide — which
    is the specific thing the whole cardBase/target dance exists to stop.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()
    with open(os.path.join(here, "static", "app.css"), encoding="utf-8") as fh:
        css = fh.read()

    check("the times row folds when expanded",
          ".collapsed-card.expanded .fstrip--lg .fstrip-ends {" in css)
    check("...only on the hero size", ".fstrip--lg .fstrip-ends {" in css)
    # max-height, not height: the zone superscript is lifted above its own
    # baseline by a transform, and a hard height would clip it.
    block = css.split(".fstrip--lg .fstrip-ends {", 1)[1].split("}", 1)[0]
    check("folded with max-height, so the zone is never clipped",
          "max-height" in block and re.search(r"[^-]height:", block) is None, block)
    check("...and it animates", "transition:" in block)
    check("...unless motion is reduced",
          ".fstrip--lg .fstrip-ends { transition: none; }" in css)

    # THE FOLD MUST BE LINEAR IN HEIGHT. Rendered height is min(natural,
    # max-height), so a 4rem -> 0 transition stands still for its first
    # third and then collapses over the rest, while the panel and spacer
    # move smoothly across all 260ms. The three stop cancelling and the
    # welded bottom edge creeps, then snaps.
    check("the fold is driven from measured pixels, not the resting cap",
          "function foldEnds(collapsed)" in html
          and "endsEl.style.maxHeight = (collapsed ? endsH : 0) + 'px'" in html)
    check("...with the start value committed before the end value",
          "void endsEl.offsetHeight;" in html)
    check("...on both directions", "foldEnds(true);" in html and "foldEnds(false);" in html)
    check("...and the pin released once the row is back",
          "if (endsEl) endsEl.style.maxHeight = '';" in html)
    check("the expanded rule no longer sets max-height itself",
          ".collapsed-card.expanded .fstrip--lg .fstrip-ends { opacity: 0; }" in css)

    check("the spacer maths subtracts the folding row when opening",
          "cardBase - endsH + target" in html)
    check("...and adds it back when closing", "cardBase + endsH" in html)
    check("the row is measured while SHUT, not mid-fold",
          "if (open) measureEnds();" in html
          and "card.classList.contains('expanded')) return;" in html)
    check("the panel cap accounts for it too", "_ptCapPanel(endsH)" in html)

    # The rule that let the city pair grow a second line the instant the
    # class flipped is gone; it jumped the card header in one frame while
    # everything below it slid over 260ms.
    check("the city pair no longer re-wraps mid-animation",
          ".collapsed-card.expanded .fstrip-cities {" not in html)


def test_map_refits_once_and_only_when_still():
    """Closing the card re-fitted the map TWICE. (1.10.1)

    .expanded comes off before the 300ms slide starts, so the old guard
    (which only checked that class) let a fit run immediately against a
    card height 300ms in the future — then the settle pass at 320ms
    measured the real height and fitted again. Two fitBounds calls with
    different padding, a third of a second apart: the map lurched out and
    back every time you hid the details.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()

    check("layout() refuses to re-fit mid-slide",
          "!window._ptSliding &&" in html)
    check("the flag is raised when an animated slide begins",
          "window._ptSliding = true;" in html)
    # Both slide directions must lower it, and the instant start-up call
    # must never raise it.
    check("both slide directions clear the flag",
          html.count("window._ptSliding = false;") >= 3)
    check("the settle pass still runs after the flag clears",
          html.index("window._ptSliding = false;") <
          html.index("if (!card.classList.contains('expanded') && window._ptLayoutHero)"))
    # lastTop must NOT be updated on a skipped fit, or the settle pass
    # would think the card had not moved and skip the real one too.
    guard = html.split("!window._ptSliding &&", 1)[1].split("}", 1)[0]
    check("lastTop is only updated when a fit actually happens",
          "lastTop = actualTop;" in guard)
    check("...and nowhere else in layout()",
          html.split("function layout(", 1)[1].split("window._ptLayoutHero", 1)[0]
          .count("lastTop = actualTop") == 1)


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


def test_map_does_not_steal_the_scroll():
    """A drag below the card's top edge scrolls the page. (1.10.2)

    The map is a fixed full-screen layer BEHIND the page, so it is still
    there below the card: in the margins either side of it, in the gap
    above the tab pills, behind anything the list does not physically
    cover. Leaflet will happily take a touch through a transparent gap, so
    scrolling toward next week's flights sometimes panned the map sideways
    instead, depending on where a thumb landed.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
        html = fh.read()

    check("the shield exists", 'class="map-shield"' in html)
    block = html.split(".map-shield {", 1)[1].split("}", 1)[0]
    check("it starts at the card's top edge, not the top of the window",
          "top: var(--card-top)" in block, block)
    check("...and runs to the bottom", "bottom: 0" in block, block)
    # Above the map (0) and below the page (20): it must steal from
    # neither. The scrim sits at 1.
    check("it sits between the map and the page", "z-index: 2" in block, block)
    check("vertical drags are declared as page scrolls",
          "touch-action: pan-y" in block, block)
    # The strip of map ABOVE the card is deliberately still pannable.
    check("the visible map is left alone", "top: 0" not in block, block)


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


def test_tracker_is_scoped_to_this_trip_and_the_next():
    """The tracker holds this trip and the next. Nothing else. (1.11.0)

    It used to render the entire 365-day roster and hide most of it behind
    a button — a list that grows without bound, pretending to be a list
    that does not. The scope is the fix; the button was the symptom.
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

    # Anchored mid-trip: that whole trip, flown legs and all, plus the next.
    w = tracker_window(roster, "b2")
    check("the anchor's whole trip is kept, including what is already flown",
          {"b1", "b2", "b3"} <= w, str(w))
    check("...and the next trip", "c1" in w, str(w))
    check("...but not the one after that", "d1" not in w, str(w))
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
    test_open_card_cannot_outgrow_the_screen()
    test_viewer_theme_is_consistent_across_pages()
    test_expanding_does_not_disturb_map_or_schedule()
    test_card_grows_upward_from_a_fixed_bottom_edge()
    test_detail_panels_slide_rather_than_snap()
    test_schedule_works_without_the_map()
    test_session_key_survives_redeploy()
    test_scheduled_time_line_is_marked_as_an_echo()
    test_scroll_reveal()
    test_show_on_map_action()
    test_settings_budget_saves()
    test_no_hardcoded_palette_colours()
    test_flight_strip_is_one_component()
    test_time_line_splits_the_zone_off()
    test_leg_switch_keeps_the_time_rows()
    test_expanded_view_is_per_airport()
    test_route_facts_are_not_measurements()
    test_arrival_source_is_in_english()
    test_live_box_does_not_swallow_the_flight_detail()
    test_strip_times_fold_when_the_panel_opens()
    test_map_refits_once_and_only_when_still()
    test_list_dropdown_follows_the_same_decisions()
    test_map_does_not_steal_the_scroll()
    test_refit_glides_rather_than_snapping()
    test_tracker_is_scoped_to_this_trip_and_the_next()
    test_list_rows_carry_delay_state()
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
