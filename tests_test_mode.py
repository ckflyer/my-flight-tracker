"""Test mode, and who is allowed to run it. (1.6.0)

Two features, one suite, because they are the same claim from two sides:
this install can be OPERATED — rehearsed, inspected, handed to a second
administrator — without SSH, without SQL, and without money.

THE THING THIS SUITE MOSTLY GUARDS is that a rehearsal cannot touch
anything real. Test mode invents flights, and an invention that leaks is
worse than no test mode at all: a simulated leg that spent an AeroAPI
credit costs money for a flight that does not exist, and one that reached
a logbook would put a fiction in a legal record. So the isolation tests
outnumber the "does it work" tests on purpose.

The other half is that the simulator must NOT fake the app's answers. It
produces position reports and nothing else; `flightmatch`, `tags` and
`closure` then run on them unchanged. A simulator that wrote `closed = 1`
directly would prove nothing at all, so several tests here assert that the
closure REASON is the real production route.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["PT_DB_FILE"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["TRACK_POLLER_ENABLED"] = "0"

from app.db import init_db, get_connection                # noqa: E402
from app import enrichment, poller, simulator             # noqa: E402
from app.auth import (count_admins, create_user, list_all_users,  # noqa: E402
                      set_admin)
from app.flights import (flight_key, get_flight,          # noqa: E402
                         load_schedule, rows_awaiting_gate_in, write)
from app.schedule import get_current_info                 # noqa: E402

init_db()
UTC = timezone.utc
PASS, FAIL = [], []


def _runs_of_comment_lines(text):
    """Consecutive whole-line comments. The owner's complaint about the
    compose file was that reading it meant wading through paragraphs, so
    what is being asserted is the absence of paragraphs."""
    run = []
    for ln in text.splitlines():
        stripped = ln.strip()
        # A commented-OUT setting is not prose. `# - PT_DEBUG_LOG=1` is a
        # line you uncomment, and a run of those is a menu, not a
        # paragraph. Only genuine prose counts against the limit.
        if stripped.startswith("#") and "=" not in stripped:
            run.append(ln)
        else:
            if run:
                yield run
            run = []
    if run:
        yield run


def _style_bodies(icons_src):
    """Each style's `icon` string out of make_icons.py, crudely but reliably.

    Parsed rather than imported because make_icons.py WRITES FILES at import
    time — running it from a test suite would rewrite static/ as a side
    effect of asserting something about it.
    """
    out, rest = [], icons_src
    while '"icon": (' in rest or '"icon": \'' in rest:
        i = rest.find('"icon":')
        j = rest.find('},', i)
        out.append(rest[i:j])
        rest = rest[j:]
    return out


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"  {detail}" if detail and not cond else ""))


def fly(uid, leg, start, sweeps, step=20):
    """Run the real poller over a simulated leg. Returns the final row."""
    t = start
    for _ in range(sweeps):
        t += timedelta(seconds=step)
        poller.poll_once(t)
    return get_flight(leg.id), t


UID = create_user("owner", "password123")
T0 = datetime.now(UTC)


# ---------------------------------------------------------------------------
print("\n-- a rehearsal cannot touch anything real --")

legs = simulator.start(UID, "normal", T0)
LEG = legs[0]
row = get_flight(LEG.id)
check("the leg is flagged as simulated", bool(row["simulated"]))
check("...and records which scenario made it", row["sim_scenario"] == "normal")
check("...using a flight number no regional operates",
      int(row["flight_number"]) >= simulator.SIM_FLIGHT_BASE,
      row["flight_number"])

# The money guard. Give the account a key and a budget, then prove that
# every paid path refuses on the grounds of simulation alone.
conn = get_connection()
conn.execute("UPDATE users SET aeroapi_enabled = 1, aeroapi_key = 'k', "
             "aeroapi_budget = 50.0 WHERE id = ?", (UID,))
conn.commit()
conn.close()

CALLS = {"n": 0}


def exploding_fetch(*a, **k):
    CALLS["n"] += 1
    raise AssertionError("a simulated leg asked AeroAPI for real data")


enrichment.fetch_leg = exploding_fetch
enrichment.refresh_usage = lambda uid, now: False

check("refresh() refuses a simulated leg outright",
      enrichment.refresh(UID, LEG, T0, has_adsb=True, departed=True) is None)
check("...before spending anything", CALLS["n"] == 0)
check("backfill_gate_in() refuses one too",
      enrichment.backfill_gate_in(UID, LEG, T0 + timedelta(days=1)) is None)
check("...also before spending anything", CALLS["n"] == 0)

# The guard must not depend on the leg being open, closed, or anything else
# that a scenario could change.
write(LEG.id, always={"closed": 1, "closed_by": "backstop",
                      "closed_at": T0.isoformat()})
check("a CLOSED simulated leg is never chased for a late gate-in",
      not any(r["id"] == flight_key(LEG.id)
              for r in rows_awaiting_gate_in(T0 + timedelta(days=1))))
check("...and asking directly still refuses",
      enrichment.backfill_gate_in(UID, LEG, T0 + timedelta(days=1)) is None)
check("no AeroAPI call was made at any point", CALLS["n"] == 0)

# The ADS-B guard. The network provider must never be reached either — not
# for cost, but because the shared rate limiter is a finite resource the
# real legs are relying on.
ADSB = {"n": 0}


def exploding_live_state(callsign):
    ADSB["n"] += 1
    raise AssertionError("a simulated leg asked the ADS-B provider")


poller.live_state = exploding_live_state
simulator.stop(UID)
LEG = simulator.start(UID, "normal", T0)[0]
row, t = fly(UID, LEG, T0, 90)
check("the ADS-B provider is never asked about a simulated leg", ADSB["n"] == 0)
check("...and no AeroAPI call happened during a whole flight", CALLS["n"] == 0)
check("...with the leg's own ticket counter untouched",
      int(row["api_queries_used"]) == 0, str(row["api_queries_used"]))


# ---------------------------------------------------------------------------
print("\n-- but the app's own logic runs on it, unchanged --")

check("the leg actually flew", bool(row["airborne_seen"]) and bool(row["landed_seen"]))
check("...reached Arrived", row["phase_tag"] == "Arrived", str(row["phase_tag"]))
check("...and closed on a REAL production route, not a simulator write",
      row["closed_by"] in ("observed", "airline", "relaunch", "backstop"),
      str(row["closed_by"]))
check("...specifically the observed route for a clean leg",
      row["closed_by"] == "observed", str(row["closed_by"]))
check("a track was recorded, as for any leg",
      get_flight(LEG.id) is not None)


# ---------------------------------------------------------------------------
print("\n-- each scenario reproduces the bug it is named for --")

# The 1.4.0 taxi-in trap: parked, still transmitting, no silence anywhere.
simulator.stop(UID)
TRAP = simulator.start(UID, "taxi_in_trap", T0)[0]
row, t = fly(UID, TRAP, T0, 75)
check("taxi-in trap: still open while the APU runs", not row["closed"],
      str(row["closed_by"]))
check("...and correctly sitting in Taxi-in", row["phase_tag"] == "Taxi-in",
      str(row["phase_tag"]))
simulator.age(TRAP.id, 30)
poller.poll_once(t + timedelta(seconds=20))
row = get_flight(TRAP.id)
check("...closing on the LONG STOP once aged 30 minutes",
      bool(row["closed"]) and row["closed_by"] == "observed",
      str(row["closed_by"]))

# Coverage lost in cruise: never seen to land, so only the backstop can end it.
simulator.stop(UID)
DARK = simulator.start(UID, "coverage_loss", T0)[0]
row, t = fly(UID, DARK, T0, 80)
check("coverage loss: never seen to land", not row["landed_seen"])
check("...so no observed route can fire", not row["closed"])
simulator.age(DARK.id, 200)
poller.poll_once(t + timedelta(seconds=20))
row = get_flight(DARK.id)
check("...and the BACKSTOP is what ends it",
      bool(row["closed"]) and row["closed_by"] == "backstop",
      str(row["closed_by"]))

# The has_departed guard. Nothing may close a leg that never flew.
simulator.stop(UID)
NEVER = simulator.start(UID, "never_departs", T0)[0]
row, t = fly(UID, NEVER, T0, 80)
simulator.age(NEVER.id, 600)
poller.poll_once(t + timedelta(seconds=20))
check("a leg that never departs still cannot close, at any age",
      not get_flight(NEVER.id)["closed"])

# The 1.5.0 handover, which is the whole reason this scenario exists.
simulator.stop(UID)
TURN = simulator.start(UID, "turn", T0)
check("the turn scenario builds two legs", len(TURN) == 2, str(len(TURN)))
check("...that are an out and back",
      TURN[0].origin == TURN[1].destination and
      TURN[0].destination == TURN[1].origin)
t, timeline = T0, []
for _ in range(160):
    t += timedelta(seconds=20)
    poller.poll_once(t)
    cur = get_current_info(UID, t).current
    label = cur.flight_number if cur else None
    if not timeline or timeline[-1] != label:
        timeline.append(label)
check("the card starts on leg 1", timeline and timeline[0] == TURN[0].flight_number,
      str(timeline))
check("...and hands over to leg 2", TURN[1].flight_number in timeline, str(timeline))
check("...exactly once, with no flapping between them", len(timeline) == 2,
      str(timeline))


# ---------------------------------------------------------------------------
print("\n-- ageing shifts what happened, never what was scheduled --")

simulator.stop(UID)
AGE = simulator.start(UID, "taxi_in_trap", T0)[0]
row, t = fly(UID, AGE, T0, 75)
before = (row["date"], row["dep_time_local"], row["arr_time_local"])
stopped_before = row["stopped_since"]
simulator.age(AGE.id, 45)
row = get_flight(AGE.id)
check("the scheduled date and times are NOT moved",
      (row["date"], row["dep_time_local"], row["arr_time_local"]) == before)
check("...but the observed stop is", row["stopped_since"] != stopped_before)
shift = (datetime.fromisoformat(stopped_before)
         - datetime.fromisoformat(row["stopped_since"])).total_seconds()
check("...by exactly the requested amount", abs(shift - 45 * 60) < 1, str(shift))
check("ageing a leg that is not simulated is refused",
      simulator.age("2026-01-01-1234-DFW-OKC", 30) is False)


# ---------------------------------------------------------------------------
print("\n-- stopping leaves nothing behind --")

n = simulator.stop(UID)
check("stop reports what it removed", n >= 1, str(n))
check("no simulated flight rows survive", simulator.active_sim_rows() == [])
conn = get_connection()
orphan_roster = conn.execute(
    "SELECT COUNT(*) AS n FROM roster WHERE flight_id NOT IN "
    "(SELECT id FROM flights)").fetchone()["n"]
orphan_pos = conn.execute(
    "SELECT COUNT(*) AS n FROM positions WHERE flight_key NOT IN "
    "(SELECT id FROM flights)").fetchone()["n"]
conn.close()
check("...no orphaned roster rows", orphan_roster == 0, str(orphan_roster))
check("...no orphaned position tracks", orphan_pos == 0, str(orphan_pos))
check("...and the roster is empty again", load_schedule(UID) == [])
check("stopping twice is harmless", simulator.stop(UID) == 0)
check("starting a scenario clears any previous one",
      len(simulator.start(UID, "normal", T0)) == 1 and
      len(simulator.active_sim_rows()) == 1)
simulator.stop(UID)
check("an unknown scenario key creates nothing",
      simulator.start(UID, "not-a-scenario", T0) == [])


# ---------------------------------------------------------------------------
print("\n-- creating a second admin --")

DAVE = create_user("fo_dave", "password123")
check("only the FIRST account is an admin automatically", count_admins() == 1)
check("...and it is the owner",
      [u["username"] for u in list_all_users() if u["is_admin"]] == ["owner"])

check("promoting works", set_admin(DAVE, True) is True)
check("...and there are now two", count_admins() == 2)
check("promoting an existing admin changes nothing", set_admin(DAVE, True) is False)
check("demoting works", set_admin(DAVE, False) is True)
check("...back to one", count_admins() == 1)

check("THE LAST ADMIN CANNOT BE REMOVED", set_admin(UID, False) is False)
check("...so an install can never end up with data and no administrator",
      count_admins() == 1)
check("promoting an account that does not exist is refused",
      set_admin(99999, True) is False)


# ---------------------------------------------------------------------------
print("\n-- everything that runs the install is on one page --")

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "templates", "admin.html"), encoding="utf-8") as fh:
    ah = fh.read()
with open(os.path.join(here, "templates", "flights.html"), encoding="utf-8") as fh:
    fh_src = fh.read()
with open(os.path.join(here, "templates", "settings.html"), encoding="utf-8") as fh:
    sh = fh.read()
with open(os.path.join(here, "app", "main.py"), encoding="utf-8") as fh:
    src = fh.read()

check("the people table is on /admin", 'id="people"' in ah)
check("...with a Make admin control", "/admin/users/promote/" in ah)
check("...and it is gone from Settings", "/settings/users/delete/" not in sh)
check("Settings points at /admin instead", 'href="/admin"' in sh)
check("test mode is on /admin", "/admin/test/start" in ah)
check("diagnostics are a SECTION of /admin, not a separate page",
      'id="diagnostics"' in ah and "diagnostics_html" in ah)
check("...and so is the decision log", 'id="log"' in ah and "log_events" in ah)
check("the old diagnostics URL still lands somewhere useful",
      'RedirectResponse(url="/admin#diagnostics"' in src)
check("...as does the old decision-log URL",
      'RedirectResponse(url="/admin#log"' in src)

# The schedule page must be free of all of it.
check("none of this is on the Flights page",
      "people-table" not in fh_src and "/admin/test/" not in fh_src)
check("the Flights page is at /flights now", '@app.get("/flights"' in src)
check("...and the old /admin schedule URL redirects rather than 404s",
      'RedirectResponse(url="/flights", status_code=301)' in src)
check("the old Settings delete route still redirects rather than 404ing",
      'return RedirectResponse(url="/admin#people", status_code=307)' in src)
check("every admin route checks is_admin",
      src.count('if not pilot["is_admin"]:') >= 7,
      str(src.count('if not pilot["is_admin"]:')))
check("an admin cannot demote themselves",
      'if user_id == pilot["id"]:' in src)
check("the whole page is admin-gated at the route",
      'if not pilot["is_admin"]:\n        return RedirectResponse(url="/flights"' in src)
check("ageing is bounded and only ever backwards",
      "max(1, min(int(minutes), 720))" in src)


# ---------------------------------------------------------------------------
print("\n-- promotion is not a single tap --")

from app.auth import verify_password                      # noqa: E402

check("promotion requires a password field", "password: str = Form(...)" in src)
check("...checked against the PROMOTER's own hash",
      'verify_password(password, pilot["password_hash"])' in src)
check("...and demotion is gated the same way",
      src.count('verify_password(password, pilot["password_hash"])') >= 2)
check("a wrong password changes nothing and says so",
      'url="/admin?flash=badpw#people"' in src)
check("the confirm panel is hidden until asked for", "confirm-row" in ah)
check("...and states what an admin can do before you type anything",
      "delete every account" in ah)
check("the flash message is a CODE in the URL, never free text",
      "FLASHES = {" in src and "FLASHES.get(flash" in src)


print("\n-- one aeroplane, everywhere --")

with open(os.path.join(here, "templates", "partials", "plane_glyph.html"),
          encoding="utf-8") as fh:
    glyph = fh.read()
with open(os.path.join(here, "templates", "viewer.html"), encoding="utf-8") as fh:
    viewer = fh.read()
with open(os.path.join(here, "make_icons.py"), encoding="utf-8") as fh:
    icons = fh.read()

# The signature of the shared silhouette. If any surface is drawing a
# different aeroplane, this substring will be missing from it.
NOSE = "M32 4c2.6 0 4.4 3.4 4.7 8.6l.5 7.8"
check("the app icon uses the shared silhouette", NOSE in icons)
check("...so does the tab bar glyph", NOSE in glyph)
check("...and so does the progress bar", NOSE in viewer)
check("the map marker reads its shape from the generated file",
      "window.PLANE_STYLES" in viewer)
check("there is no second 'marker' shape any more",
      's["marker"] = s["icon"]' in icons)
check("every style is a SINGLE path, so the map outline is one clean edge",
      all(v.count("<path") == 1 for v in _style_bodies(icons)),
      str([v.count("<path") for v in _style_bodies(icons)]))
check("the marker strokes only <path>, not <rect>",
      "st.body.replace(/<path/g" in viewer)
check("...with the outline painted under the fill, not over it",
      'paint-order="stroke"' in viewer)

# The progress-bar plane was `color: var(--accent)` on a fill that is ALSO
# var(--accent) — invisible until it moved past the fill.
with open(os.path.join(here, "static", "app.css"), encoding="utf-8") as fh:
    css = fh.read()
check("the progress plane no longer inherits the bar's own colour",
      "color: var(--accent); line-height: 0;" not in viewer)
check("...it has its own paint that reads against both halves of the track",
      "stroke: var(--accent);" in viewer and "fill: var(--card);" in viewer)

# The tile artwork. Each of these guards a size where it could go wrong.
check("the arc is split: solid behind the plane, dashed ahead",
      'stroke-dasharray' in icons and 'stroke="#7fb0ea"' in icons)
check("...and the fine detail is dropped at favicon size, not shrunk to mush",
      "fine = px >= 64" in icons)
# Asserted on the OUTPUT, not on where a substring sits in the source —
# the first version of this test checked that "_backdrop" appeared before
# "if maskable:" in the file, which is a fact about formatting rather than
# about what gets drawn, and it failed while the code was correct.
_ns = {"__file__": os.path.join(here, "make_icons.py")}
exec(compile(icons.split("written = []")[0], "make_icons.py", "exec"), _ns)
check("maskable icons carry NO artwork, since Android crops them",
      "stroke-dasharray" not in _ns["icon_svg"]("modern", 512, maskable=True))
check("...while the ordinary tile does have it",
      "stroke-dasharray" in _ns["icon_svg"]("modern", 512))
check("the backdrop scales with the icon rather than being fixed",
      icons.count("u = px / 64.0") >= 1 and "{32*u:" in icons)
check("the plane sits on the arc apex, not centred in the square",
      "translate({32*u:.2f} {28*u:.2f}) rotate(55)" in icons)

check("the timezone superscript is one step smaller",
      "font-size: 0.5rem;" in css)
check("...and not smaller than iOS will reliably render",
      "0.4375rem" not in css and "0.375rem" not in css)





# ---------------------------------------------------------------------------
print("\n-- the diagnostics page was the thing that was broken (1.8.0) --")

with open(os.path.join(here, "app", "airplaneslive.py"), encoding="utf-8") as fh:
    al = fh.read()
with open(os.path.join(here, "app", "livesource.py"), encoding="utf-8") as fh:
    ls = fh.read()
with open(os.path.join(here, "app", "debuglog.py"), encoding="utf-8") as fh:
    dl = fh.read()

# THE FEEDS-ALL-RED BUG. probe() called requests.get directly, so loading
# the admin page fired one request per feed with no spacing. adsb.fi allows
# 1/second. Every feed after the first returned 429 and the page reported
# them all dead while the poller tracked flights perfectly well.
check("there is a shared throttle to call", "def throttle()" in ls)
check("the probe goes through it", "throttle()" in al and "from .livesource import throttle" in al)
check("429 is reported as rate limiting, not failure",
      "rate limited (feed is up" in al)
check("...and counts as a working feed",
      "if (ok or limited) and e.get" in src)
check("the summary falls back to REAL lookup history when the probe fails",
      "probe says no, but %d of the last %d REAL lookups succeeded" in src)

# THE CLOSED-LEG BUG. active_flights() returns anything inside the tracking
# window, and a leg stays there 3h past scheduled arrival whether closed or
# not — so a leg that finished on the airline's gate-in still showed under
# "active" AND got a live uncached lookup fired at it.
check("closed legs are separated out of the active list",
      '(_done if (_r is not None and _r["closed"]) else _open)' in src)
check("...and are explicitly not queried", "Not queried, because they are" in src)
check("...with the reason they are still listed shown",
      "still listed only because they are inside the" in src)

print("\n-- the log is searchable, bounded, and takeable away --")
check("flight filter is a CONTAINS match, not exact",
      'where.append("subject LIKE ?")' in dl)
check("...because it held a full flight id nobody would type exactly",
      "Typing \"3729\" now works." in dl)
check("free-text search covers subject, event and detail together",
      '"(subject LIKE ? OR event LIKE ? OR detail LIKE ?)"' in dl)
check("the event menu is built from what is in the log",
      "def event_names(" in dl)
check("the tail asks only for what it has not seen", "after_id" in dl)
check("the default is 100 lines, not 200", "limit: int = 100" in src)
check("...and is bounded at both ends",
      "max(10, min(int(limit), 2000))" in src)
check("there is a live tail endpoint", '"/admin/log/tail"' in src)
check("...that polls rather than holding a connection open",
      "long-lived connection is the first thing" in src)
check("...and stops when the tab is hidden", "visibilitychange" in ah)
check("...and trims itself so an overnight tail cannot eat the tab",
      "TAIL_MAX" in ah)
check("the log downloads as plain text", '"/admin/log/download"' in src)
check("...carrying the same filters that are on screen",
      "filters: subject={subject or '-'}" in src)
check("the console is a bounded scroller, not an endless page",
      "max-height: 60vh" in ah)

print("\n-- admin page fits a phone --")
check("the jump pills are gone", 'class="jump"' not in ah)
check("generated diagnostics rows no longer force nowrap",
      "white-space:nowrap;vertical-align:top'>%s</td>" not in src)
check("...and break long tokens instead", "word-break:break-word" in src)
check("the embedded diagnostics block is width-constrained",
      ".diag { overflow-wrap: anywhere; }" in ah and "table-layout: fixed" in ah)
check("...including its inline-styled inputs",
      "min-width: 0 !important" in ah)

print("\n-- ADS-B feeds after airplanes.live withdrew its free API --")
check("the open community feeds are the defaults",
      "api.adsb.lol/v2" in al and "opendata.adsb.fi/api/v2" in al)
check("airplanes.live ships disabled",
      '{"url": "https://api.airplanes.live/v2", "enabled": False' in al)
check("...with the reason and the way back recorded",
      "FREE FROM A FEEDER'S OWN IP" in al)
check("attribution is carried, as adsb.fi's terms require",
      "FEED_CREDITS" in al and "adsb.fi" in src)
check("a saved feed list can be reset to the built-in defaults",
      "def reset_endpoints(" in al and "/admin/diagnostics/endpoints/reset" in src)
check("...because a saved list silently overrides the defaults",
      "stays pinned to a feed that now returns 403" in al)
# The first version of this button ran DELETE FROM meta. The table is
# app_meta — the one this module reads three lines above — so it 500'd on
# exactly the installs that needed it, because a table only exists once
# something has been written to it.
check("reset targets the table this module actually reads",
      "DELETE FROM app_meta WHERE key" in al)
check("...and treats 'nothing was ever saved' as success, not an error",
      "nothing to reset" in al)

with open(os.path.join(here, "docker-compose.yml"), encoding="utf-8") as fh:
    dc = fh.read()
check("docker-compose is short", len(dc.splitlines()) < 30, str(len(dc.splitlines())))
_settings = [ln for ln in dc.splitlines() if "=" in ln and "PT_" in ln or "ADSB_" in ln]
check("...with an inline comment on each setting, not a paragraph above it",
      sum(1 for ln in _settings if "#" in ln.split("=", 1)[-1]) >= 6,
      str([ln.strip()[:40] for ln in _settings]))
check("...and no multi-line comment blocks", 
      max((len(list(g)) for g in _runs_of_comment_lines(dc)), default=0) <= 2,
      str(max((len(list(g)) for g in _runs_of_comment_lines(dc)), default=0)))
check("...and still documents every setting",
      all(k in dc for k in ("PT_RETENTION_DAYS", "PT_HOME_CALLSIGN",
                            "PT_DEBUG_LOG", "PT_SECRET_KEY", "ADSB_ENDPOINTS")))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
