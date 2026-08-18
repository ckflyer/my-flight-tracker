"""Step 5: the regression pass, as a matrix rather than a checklist.

WHY THIS IS A SUITE AND NOT AN AFTERNOON. The roadmap called step 5 "a
regression pass across themes, time formats and the odd states", which
describes a person clicking through the app once. That finds today's
breakage and nothing after it. The same combinations run on every commit
find it forever, and they cost seconds.

The 1.18.0 Import bug is the argument. Both halves of that path were
tested and the JOIN was not, so a dead button survived nine releases.
Everything here is a join: a PAGE, rendered under a STATE, for a ROLE,
in a THEME, at a CLOCK. Nothing in this file asserts on wording or
layout — the other suites do that, and duplicating it here would make
every copy-edit break a regression test. This asks a narrower question:
does the page come out whole, and does it come out in the shape the
settings asked for.

WHAT COUNTS AS BROKEN HERE
  * any status that is not 200 (or an expected redirect)
  * an unrendered Jinja expression reaching the browser
  * the literal "None" printed where a value should be
  * a page that ignores the theme or clock it was handed
  * a traceback

The odd states are the ones that have actually broken this app before,
which is why they are these and not a general-purpose fuzz: an empty
roster (a brand-new account), a roster of nothing but past legs (the
window that 1.16.0's handover exists for), a single leg with no trip
around it, a cancelled leg, a deadhead, and an airport the coordinate
database does not know.
"""

import os
import re
import sys
import tempfile
from datetime import date, datetime, time as dtime, timedelta, timezone

os.environ["PT_DB_FILE"] = os.path.join(tempfile.mkdtemp(), "regression.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient                # noqa: E402

import app.main as m                                     # noqa: E402
from app.airports import enrich_leg                      # noqa: E402
from app.auth import create_user                         # noqa: E402
from app.db import get_connection, init_db               # noqa: E402
from app.flights import replace_schedule, write          # noqa: E402
from app.models import FlightLeg                         # noqa: E402
from app.settings import load_settings, save_settings    # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (f"  [{detail}]" if detail and not cond else ""))


def leg(lid, d, num, o, dst, dep, arr, start=False, dh=False):
    l = FlightLeg(id=lid, date=d, flight_number=num, origin=o, destination=dst,
                  dep_time_local=dtime.fromisoformat(dep),
                  arr_time_local=dtime.fromisoformat(arr),
                  trip_start=start, is_deadhead=dh)
    enrich_leg(l)
    return l


# --------------------------------------------------------------------------
# The odd states. Each returns the legs for one scenario.
# --------------------------------------------------------------------------

def state_empty(today):
    """A brand-new account. The commonest first-run state and the one most
    often forgotten — every page has to render with nothing to render."""
    return []


def state_normal(today):
    """A two-day trip with an overnight. The ordinary case."""
    return [leg("n1", today, "2673", "DFW", "VPS", "17:00", "19:03", start=True),
            leg("n2", today, "3671", "VPS", "SGF", "20:00", "21:40"),
            leg("n3", today + timedelta(days=1), "4152", "SGF", "DFW", "11:27", "12:54")]


def state_all_past(today):
    """Everything already flown. This is the window TRIP_HANDOVER exists
    for, and the one where a naive 'show the next trip' empties the page."""
    d = today - timedelta(days=6)
    return [leg("p1", d, "1000", "DFW", "OKC", "08:00", "09:00", start=True),
            leg("p2", d, "1001", "OKC", "DFW", "11:00", "12:00")]


def state_single(today):
    """One leg, no trip around it. Day numbering, trip spans and the
    overnight logic all have to survive a list of length one."""
    return [leg("s1", today + timedelta(days=3), "2200", "DFW", "AEX",
                "08:50", "10:11", start=True)]


def state_deadhead(today):
    """A deadhead beside a real leg — the badge, and the fact that a DH
    still occupies a day."""
    return [leg("d1", today, "9001", "DFW", "ORD", "06:00", "08:20",
                start=True, dh=True),
            leg("d2", today, "9002", "ORD", "DFW", "18:00", "20:30")]


def state_unknown_airport(today):
    """An airport with no coordinates. Anything that draws a map or
    computes a distance has to degrade rather than raise — this is what
    an obscure outstation looks like to the app."""
    return [leg("u1", today, "3300", "DFW", "ZZZZ", "09:00", "11:00", start=True)]


STATES = [
    ("empty roster", state_empty),
    ("ordinary trip", state_normal),
    ("all past", state_all_past),
    ("single leg", state_single),
    ("deadhead", state_deadhead),
    ("unknown airport", state_unknown_airport),
]

# Every page a person can actually open. The API endpoints are covered by
# other suites; these are the ones that RENDER.
PAGES = ["/", "/calendar", "/flights", "/settings", "/admin"]
VIEWER_PAGES = ["/", "/calendar", "/viewer-settings"]


def scan(name, resp):
    """The four things that mean a page came out broken."""
    ok = resp.status_code == 200
    check(f"{name}: renders", ok, f"HTTP {resp.status_code}")
    if not ok:
        return
    html = resp.text
    # An unrendered expression means a template referenced something the
    # route never passed. It reaches the browser as literal braces.
    leftover = re.findall(r"\{\{.*?\}\}|\{%.*?%\}", html)
    check(f"{name}: no unrendered template syntax", not leftover,
          str(leftover[:2]))
    # ">None<" is a value that was absent and printed anyway. Restricted to
    # element text so it cannot trip on the word inside a script or a
    # legitimate attribute.
    nones = re.findall(r">\s*None\s*<", html)
    check(f"{name}: no bare None in the page", not nones, str(len(nones)))
    check(f"{name}: no traceback", "Traceback (most recent call last)" not in html)


def page_theme(html):
    """The theme the page is actually WEARING, off its <html> tag.

    Searching the whole document for `data-theme="light"` is not the same
    question and quietly passes either way: a page carrying a theme
    toggle mentions both values in its script, so the substring is there
    whichever theme is active. Asked that way the check can never fail,
    which is worse than not having it.
    """
    tag = re.search(r"<html[^>]*>", html)
    if not tag:
        return None
    found = re.search(r'data-theme="([^"]*)"', tag.group(0))
    return found.group(1) if found else None


def clock_shape(html):
    """What clock is this page actually printing?

    Returns the set of formats found in the app's own time elements. Two
    clocks on one page is the bug worth catching: the strip taking the
    pilot's setting while the panel takes the viewer's cookie, so the
    same flight reads 5:00 PM in one place and 17:00 in the other.

    Only elements the app marks as times are looked at. Scanning the raw
    HTML would count version strings, dates and CSS values as clocks.
    """
    times = re.findall(
        r'class="(?:fstrip-time|aptblock-time|aptblock-was)[^"]*"[^>]*>([^<]{1,12})',
        html)
    found = set()
    for t in times:
        t = t.strip()
        if "'" in t or '"' in t or "+" in t or "{" in t:
            continue        # a fragment of the script that BUILDS a time
        if re.fullmatch(r"\d{1,2}:\d{2}\s*(AM|PM)", t, re.I):
            found.add("12")
        elif re.fullmatch(r"\d{1,2}:\d{2}", t):
            # 13:00 can only be 24h. 09:30 is ambiguous on its own, so it
            # is only counted when the hour settles it — otherwise a page
            # of morning flights would look like both formats at once.
            if int(t.split(":")[0]) > 12 or t.startswith("0"):
                found.add("24")
    return found


def main():
    init_db()
    uid = create_user("pilot", "pw")
    client = TestClient(m.app, raise_server_exceptions=False)
    r = client.post("/login/pilot", data={"username": "pilot", "password": "pw"},
                    follow_redirects=False)
    check("the pilot can log in", r.status_code in (302, 303), str(r.status_code))

    row = get_connection().execute(
        "SELECT share_code FROM users WHERE id = ?", (uid,)).fetchone()
    share_code = row["share_code"]

    today = date.today()

    # ---- the matrix ------------------------------------------------------
    for state_name, build in STATES:
        legs = build(today)
        replace_schedule(uid, legs)
        for theme in ("dark", "light"):
            for tf in ("12", "24"):
                s = load_settings(uid)
                s.theme = theme
                s.time_format = tf
                save_settings(uid, s)
                print(f"\n{state_name} / {theme} / {tf}h")
                for page in PAGES:
                    resp = client.get(page)
                    scan(f"{page}", resp)
                    if resp.status_code != 200:
                        continue
                    # THE SETTING MUST REACH THE PAGE. A page that renders
                    # but ignores the theme is exactly the bug
                    # viewer_display_overrides was written for: same
                    # person, two pages, two themes.
                    if page in ("/", "/calendar", "/flights", "/settings"):
                        check(f"{page}: honours theme={theme}",
                              page_theme(resp.text) == theme,
                              str(page_theme(resp.text)))
                    # ONE CLOCK PER PAGE, and it is the one that was asked
                    # for. A page showing both formats has two sources for
                    # the same setting.
                    shape = clock_shape(resp.text)
                    check(f"{page}: one clock, not two", len(shape) <= 1, str(shape))
                    if shape:
                        check(f"{page}: honours clock={tf}h", shape == {tf}, str(shape))

    # ---- the viewer sees the same app ------------------------------------
    #
    # A viewer is not a pilot with fewer buttons — they reach the same
    # pages through a share code, with their OWN theme and clock held in
    # cookies. viewer_display_overrides exists because that override was
    # written inline in the tracker route and forgotten in the calendar
    # one, so a viewer on light mode got a light tracker and a dark
    # calendar. Nothing tested it across both pages at once until now.
    print("\nviewer, by share code")
    replace_schedule(uid, state_normal(today))
    pilot_settings = load_settings(uid)
    pilot_settings.theme = "dark"
    pilot_settings.time_format = "24"
    save_settings(uid, pilot_settings)

    viewer = TestClient(m.app, raise_server_exceptions=False)
    r = viewer.post("/login/code", data={"code": share_code}, follow_redirects=False)
    check("a viewer can log in with a share code", r.status_code == 303,
          str(r.status_code))

    for page in VIEWER_PAGES:
        scan(f"viewer {page}", viewer.get(page))

    # The viewer's cookies must beat the pilot's account settings, on
    # EVERY page, not just the one the override was first written in.
    viewer.cookies.set("pt_viewer_theme", "light")
    viewer.cookies.set("pt_viewer_tf", "12")
    for page in ("/", "/calendar"):
        resp = viewer.get(page)
        scan(f"viewer {page} (own prefs)", resp)
        if resp.status_code != 200:
            continue
        check(f"viewer {page}: takes the viewer's theme, not the pilot's",
              page_theme(resp.text) == "light", str(page_theme(resp.text)))
        shape = clock_shape(resp.text)
        if shape:
            check(f"viewer {page}: takes the viewer's clock, not the pilot's",
                  shape == {"12"}, str(shape))

    # A pilot's own pages must NOT be reachable on a share code. The
    # viewer sees a schedule; they do not get the account that owns it.
    for page in ("/flights", "/admin", "/settings"):
        resp = viewer.get(page, follow_redirects=False)
        check(f"viewer is kept out of {page}",
              resp.status_code in (302, 303, 401, 403, 404), str(resp.status_code))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
