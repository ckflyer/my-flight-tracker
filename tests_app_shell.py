"""The v1.0.0 surface: install shell, versioning, schema guard, rebrand.

Every check here exists because the thing it guards fails SILENTLY. That is
the common thread and the reason this suite is worth its runtime:

  * A missing manifest link does not error. The page renders perfectly and
    "Add to Home Screen" quietly produces a bookmark with no icon. Through
    v7.4 this was the state of EIGHT of ten templates, including the login
    page — the first screen a family member ever sees.

  * A service worker whose cache name does not change on deploy does not
    error either. It serves the previous build's CSS and JavaScript forever,
    and the symptom is indistinguishable from "the server didn't update".

  * An old build opening a newer database does not error. It writes rows
    that drop the columns it does not know about, and the damage surfaces
    weeks later.

  * A carrier name left in a template does not error. It just ships.

Run: python tests_app_shell.py
"""
import glob
import os
import re
import sys
import tempfile

os.environ["PT_DB_FILE"] = os.path.join(tempfile.mkdtemp(), "shell_test.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient                # noqa: E402

import app.main as m                                     # noqa: E402
from app.db import get_meta, init_db, set_meta           # noqa: E402
from app.flights import RETENTION_DAYS                   # noqa: E402
from app.track import TRACK_RETENTION_DAYS               # noqa: E402
from app.version import (                                # noqa: E402
    API_VERSION, MIN_CLIENT_VERSION, SCHEMA_VERSION, VERSION,
    client_is_supported, version_tuple,
)

PASS, FAIL = [], []
HERE = os.path.dirname(os.path.abspath(__file__))


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (f"  [{detail}]" if detail and not cond else ""))


def main():
    init_db()
    client = TestClient(m.app)

    # -- every template is installable ------------------------------------
    print("\nInstall shell on every page")
    templates = sorted(glob.glob(os.path.join(HERE, "templates", "*.html")))
    check("found the templates", len(templates) >= 10, f"{len(templates)}")
    for path in templates:
        name = os.path.basename(path)
        src = open(path).read()
        check(f"{name} includes the app shell", 'partials/app_shell.html' in src)
        check(f"{name} registers the service worker",
              'partials/sw_register.html' in src)

    # The shell owns these now. A second copy left behind in a page is not a
    # cosmetic duplicate: two theme-color tags is a coin flip over which wins.
    print("\nNo page redeclares what the shell owns")
    for path in templates:
        name = os.path.basename(path)
        src = open(path).read()
        body = src.replace('{% include "partials/app_shell.html" %}', "")
        check(f"{name} has no stray theme-color",
              'name="theme-color"' not in body)
        check(f"{name} has no stray manifest link",
              'rel="manifest"' not in body)

    # -- the rebrand cannot regress ---------------------------------------
    print("\nNo carrier names, no old product name")
    banned = re.compile(r"pilot tracker|envoy|american eagle", re.I)
    scan = (glob.glob(os.path.join(HERE, "templates", "*.html")) +
            glob.glob(os.path.join(HERE, "templates", "partials", "*.html")) +
            glob.glob(os.path.join(HERE, "static", "*.json")) +
            glob.glob(os.path.join(HERE, "static", "*.js")))
    for path in scan:
        hits = banned.findall(open(path).read())
        check(f"{os.path.basename(path)} is unbranded", not hits, str(hits[:3]))

    # ICAO prefixes are the exception and must survive: they are operator
    # CONFIGURATION, not branding. Deleting them breaks deadhead resolution
    # outright, which is a much worse outcome than naming an airline in a
    # config default nobody sees.
    print("\nCarrier config survives the rebrand")
    from app import carriers
    check("home prefix is set", bool(carriers.HOME_PREFIX), carriers.HOME_PREFIX)
    check("home prefix leads the candidate list",
          carriers.CANDIDATE_PREFIXES[0] == carriers.HOME_PREFIX,
          str(carriers.CANDIDATE_PREFIXES))
    check("callsign builds from the home prefix",
          carriers.home_callsign("3729") == f"{carriers.HOME_PREFIX}3729")
    check("carrier.py still re-exports the list",
          __import__("app.carrier", fromlist=["x"]).CANDIDATE_PREFIXES
          == carriers.CANDIDATE_PREFIXES)

    # -- version numbers order correctly ----------------------------------
    print("\nVersion ordering")
    check("VERSION is semver", re.fullmatch(r"\d+\.\d+\.\d+", VERSION), VERSION)
    # The whole reason the scheme changed. Under the old decimal scheme this
    # comparison was FALSE, and every version-dependent decision inverted.
    check("1.10.0 is newer than 1.9.0",
          version_tuple("1.10.0") > version_tuple("1.9.0"))
    check("1.0.10 is newer than 1.0.9",
          version_tuple("1.0.10") > version_tuple("1.0.9"))
    check("2.0.0 is newer than 1.99.99",
          version_tuple("2.0.0") > version_tuple("1.99.99"))
    check("a garbage version parses instead of raising",
          version_tuple("banana") == (0, 0, 0))
    check("a garbage version reads as unsupported",
          not client_is_supported("banana"))
    check("the current build supports itself", client_is_supported(VERSION))
    check("a client below the floor is unsupported",
          not client_is_supported("0.0.1"))

    # -- /api/v1/meta ------------------------------------------------------
    print("\nClient support endpoint")
    r = client.get("/api/v1/meta")
    check("meta responds", r.status_code == 200, str(r.status_code))
    body = r.json()
    check("meta reports the build", body["version"] == VERSION)
    check("meta reports the api version", body["api_version"] == API_VERSION)
    check("meta reports the client floor",
          body["min_client_version"] == MIN_CLIENT_VERSION)
    check("no client parameter reads as supported", body["supported"] is True)
    check("an old client reads as unsupported",
          client.get("/api/v1/meta?client=0.0.1").json()["supported"] is False)
    check("meta needs no login", "error" not in body)

    # -- API routes: versioned and legacy both mounted --------------------
    print("\nAPI routing")
    paths = {getattr(r_, "path", "") for r_ in m.app.routes}
    for leaf in ("current", "selected"):
        check(f"/api/v1/{leaf} is mounted", f"/api/v1/{leaf}" in paths)
        # The alias is what stops a deploy breaking a page a family member
        # already has open. Removing it is a MAJOR version change.
        check(f"/api/{leaf} alias survives", f"/api/{leaf}" in paths)
    check("/api/v1/leg is mounted", "/api/v1/leg/{leg_id}" in paths)
    check("/api/leg alias survives", "/api/leg/{leg_id}" in paths)

    # -- service worker ----------------------------------------------------
    print("\nService worker")
    r = client.get("/sw.js")
    check("sw.js serves from the ROOT", r.status_code == 200, str(r.status_code))
    src = r.text
    # Scope: a worker served from /static/ can only control /static/, so it
    # would never see a navigation or an API call — i.e. it would do nothing.
    check("sw.js is javascript",
          "javascript" in r.headers.get("content-type", ""))
    check("the version placeholder was substituted",
          "__APP_VERSION__" not in src)
    check("the running version is baked in", f"'{VERSION}'" in src)
    # Without this the cache never rotates and update.sh stops reaching phones.
    check("the cache name is keyed to the version", "'mypilot-v' + VERSION" in src)
    check("the worker is itself uncacheable",
          "no-store" in r.headers.get("cache-control", ""))
    check("auth paths are excluded from caching", "'/login'" in src)
    check("stale API responses are tagged", "X-MyPilot-Stale" in src)
    check("offline fallback page exists",
          os.path.exists(os.path.join(HERE, "static", "offline.html")))

    # -- manifest ----------------------------------------------------------
    print("\nWeb manifest")
    r = client.get("/manifest.webmanifest")
    check("manifest responds", r.status_code == 200, str(r.status_code))
    mf = r.json()
    check("manifest is branded MyPilot", mf["name"] == "MyPilot")
    # Was #1e3a8a against a #0f1419 background, which flashed blue on launch.
    check("theme_color matches the app background",
          mf["theme_color"] == "#0f1419" == mf["background_color"])
    check("manifest declares three icons", len(mf["icons"]) == 3)
    check("a maskable icon is declared",
          any(i.get("purpose") == "maskable" for i in mf["icons"]))
    for icon in mf["icons"]:
        rel = icon["src"].lstrip("/")
        check(f"{os.path.basename(rel)} exists on disk",
              os.path.exists(os.path.join(HERE, rel)))

    # Every selectable style must have every file, or choosing it yields a
    # blank tile — a failure the user sees only after reinstalling.
    print("\nIcon styles are complete")
    for style in m.ICON_STYLES:
        for suffix in ("192.png", "512.png", "maskable-512.png"):
            f = os.path.join(HERE, "static", f"icon-{style}-{suffix}")
            check(f"icon-{style}-{suffix} exists", os.path.exists(f))
        f = os.path.join(HERE, "static", f"apple-touch-icon-{style}.png")
        check(f"apple-touch-icon-{style}.png exists", os.path.exists(f))
    check("the default style is selectable", m.DEFAULT_ICON_STYLE in m.ICON_STYLES)

    # The map marker and the app icon are generated from one source. If these
    # drift, the plane on the map stops matching the plane on the phone.
    print("\nMarker and icon share one source")
    planes = os.path.join(HERE, "static", "planes.js")
    check("planes.js exists", os.path.exists(planes))
    pj = open(planes).read()
    for style in m.ICON_STYLES:
        check(f"planes.js defines {style}", f'"{style}"' in pj)
    viewer = open(os.path.join(HERE, "templates", "viewer.html")).read()
    check("viewer.html loads planes.js", "planes.js" in viewer)
    check("viewer.html reads the shared styles", "PLANE_STYLES" in viewer)
    check("viewer.html has a marker fallback if planes.js fails",
          "styles.modern" in viewer)

    # -- connection banner -------------------------------------------------
    print("\nConnection banner")
    check("the banner exists in the page", 'id="conn-bar"' in viewer)
    check("the banner is announced to screen readers",
          'aria-live="polite"' in viewer)
    for state in ("offline", "unreachable", "stale"):
        check(f"the {state} state is handled", f"'{state}'" in viewer)
    # navigator.onLine reports true on captive portals and while the server
    # is down — exactly the airport-wifi case — so the banner must be driven
    # by whether the poll actually succeeded.
    check("the failed-poll path sets a state",
          "setConnState(navigator.onLine ? 'unreachable' : 'offline')" in viewer)
    css = open(os.path.join(HERE, "static", "app.css")).read()
    check("the banner is styled", ".conn-bar" in css)
    check("the picker is styled", ".icon-picker" in css)

    # -- retention ---------------------------------------------------------
    print("\nRetention")
    check("flights are kept a year", RETENTION_DAYS == 365, str(RETENTION_DAYS))
    # A track outliving its flight row, or the reverse, is how half-deleted
    # legs happen — a route with no card, or a card with no path.
    check("tracks are kept exactly as long as flights",
          TRACK_RETENTION_DAYS == RETENTION_DAYS,
          f"{TRACK_RETENTION_DAYS} vs {RETENTION_DAYS}")

    # -- schema version ----------------------------------------------------
    print("\nSchema version and the downgrade guard")
    check("the database stamps its schema version",
          get_meta("schema_version") == str(SCHEMA_VERSION),
          get_meta("schema_version"))
    init_db()
    check("re-running init_db is idempotent",
          get_meta("schema_version") == str(SCHEMA_VERSION))
    set_meta("schema_version", str(SCHEMA_VERSION + 5))
    try:
        init_db()
        check("an older build refuses a newer database", False, "no error raised")
    except RuntimeError as exc:
        check("an older build refuses a newer database", True)
        check("the refusal explains what to do", "backup" in str(exc).lower())
    set_meta("schema_version", str(SCHEMA_VERSION))

    # -- debug log ---------------------------------------------------------
    print("\nDecision log")
    from app import debuglog
    check("the table exists wherever the database does",
          debuglog.recent(1) is not None)
    prev = debuglog.ENABLED
    debuglog.ENABLED = True
    debuglog.log("test.event", subject="LEG-X", stopped_for_s=2700,
                 aeroapi_key="sk-MUST-NOT-APPEAR", session_token="nope")
    got = debuglog.recent(5, subject="LEG-X")
    check("an event is recorded", len(got) >= 1, str(len(got)))
    if got:
        d = got[0]["detail"]
        # The rule is not to pass secrets; this is the backstop for when
        # somebody does anyway.
        check("a key-like field is redacted", d.get("aeroapi_key") == "<redacted>",
              str(d.get("aeroapi_key")))
        check("a token-like field is redacted", d.get("session_token") == "<redacted>")
        check("ordinary values survive", d.get("stopped_for_s") == 2700)
    check("filtering by event prefix works",
          len(debuglog.recent(5, event="test.")) >= 1)
    # A poll must never fail because a diagnostic row could not be written.
    try:
        debuglog.log("test.event", subject="LEG-X", weird=object())
        check("an unserialisable value does not raise", True)
    except Exception as exc:
        check("an unserialisable value does not raise", False, str(exc))
    debuglog.ENABLED = False
    before = len(debuglog.recent(50, subject="LEG-Y"))
    debuglog.log("test.event", subject="LEG-Y")
    check("nothing is written while disabled",
          len(debuglog.recent(50, subject="LEG-Y")) == before)
    debuglog.ENABLED = prev
    check("clearing works", debuglog.clear() >= 0)

    print("\nDebug page")
    check("the template exists",
          os.path.exists(os.path.join(HERE, "templates", "debug.html")))
    paths = {getattr(r_, "path", "") for r_ in m.app.routes}
    check("/admin/debug is mounted", "/admin/debug" in paths)
    check("/admin/debug/clear is mounted", "/admin/debug/clear" in paths)
    dbg = open(os.path.join(HERE, "templates", "debug.html")).read()
    check("the debug page carries the install shell",
          'partials/app_shell.html' in dbg)
    check("it says so when logging is off", "Logging is off" in dbg)

    print("\nProgress strip hides when there is no progress")
    check("the server-rendered strip is gated",
          "current.progress_pct is not none or current.phase_tag == 'Arrived'" in viewer)
    # Without the matching JS rule the strip renders hidden and is then
    # un-hidden, empty, by the first poll.
    check("the live update applies the same rule",
          "wrap.style.display = (known || phaseTag === 'Arrived')" in viewer)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
