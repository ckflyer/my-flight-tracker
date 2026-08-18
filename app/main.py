from fastapi import FastAPI, Request, Form
from fastapi.responses import (HTMLResponse, RedirectResponse, JSONResponse,
                               PlainTextResponse, Response)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pathlib import Path
import json
from datetime import datetime, date, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo
from typing import Optional
import calendar as cal_module
from types import SimpleNamespace
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .schedule import (load_schedule, get_current_info, delete_leg,
                       merge_schedule, remove_legs)
from .importer import (ADDED, CHANGED, REMOVED, UNCHANGED, build_diff,
                       month_labels, months_covered)
from .enrichment import query_stats, budget_state
from .flights import get_flight, flight_key
from .models import FlightLeg
from .parser import parse_schedule_text
from .airports import enrich_leg
from .geo import haversine_nm
from .settings import load_settings, save_settings, AppSettings
from .track import get_breadcrumb
from . import tags
from . import view as flight_view
from .view import short_zone, zone_label  # THE zone label, see view.py
from .auth import (
    get_or_create_secret_key, count_users, create_user, get_user_by_username,
    get_user_by_id, get_user_by_share_code, verify_password, regenerate_share_code,
    list_all_users, delete_user, set_admin, set_recovery_code,
    reset_password_with_recovery_code,
)
from . import simulator
from markupsafe import Markup

# One-word codes in the URL rather than the message itself, so a redirect
# cannot be used to put arbitrary text on an admin's screen.
FLASHES = {
    "badpw": "That password was not correct. Nothing was changed.",
    "promoted": "Done — they are now an admin.",
    "demoted": "Done — admin removed.",
    "lastadmin": "Refused: that is the last admin on this install.",
}
FLASH_KIND = {"promoted": "good", "demoted": "good"}
from .ratelimit import check_rate_limit
from . import debuglog
from .version import VERSION, API_VERSION, MIN_CLIENT_VERSION, client_is_supported

BASE = Path(__file__).resolve().parent.parent
jinja_env = Environment(
    loader=FileSystemLoader(str(BASE / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
)
jinja_env.globals["version"] = VERSION

app = FastAPI(title="MyPilot")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.middleware("http")
async def _no_stale_html(request: Request, call_next):
    """Pages must never be served from cache; assets always may.

    Every asset URL carries ?v={VERSION}, so a new build asks for new
    filenames and old copies can be cached hard. The HTML has no such
    handle: the browser decides on its own how long to keep it, and mobile
    Safari in particular will happily hand back a page from before the last
    deploy. That produced a genuinely confusing bug report — a fix worked on
    desktop and appeared to do nothing on a phone, because the phone was
    still running the previous version's markup and script.

    The footer prints VERSION for exactly this reason. If it disagrees
    across two devices, one of them is stale.
    """
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


app.add_middleware(
    SessionMiddleware,
    secret_key=get_or_create_secret_key(),
    session_cookie="pt_session",
    max_age=60 * 60 * 24 * 365,  # a year — sessions are meant to be persistent
    same_site="lax",
)


@app.on_event("startup")
async def _start_track_poller():
    """Record tracks for active flights even with nobody watching.

    The container runs a single uvicorn worker, so this is one poller per
    deployment. If workers are ever added, this would start one per worker
    and they'd poll the same flights redundantly — the shared cache in
    livesource would absorb most of it, but the right fix then is to move
    this to a separate process.
    """
    from .poller import start as start_poller
    start_poller()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def current_pilot(request: Request) -> Optional[dict]:
    """Returns the logged-in pilot's user row, or None."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return get_user_by_id(user_id)


def current_viewer_user_id(request: Request) -> Optional[int]:
    """Returns the user_id a viewer session is watching, but only if the
    code they logged in with still matches that account's *current* share
    code — so regenerating the code immediately invalidates anyone still
    on the old one, even mid-session."""
    viewer_user_id = request.session.get("viewer_user_id")
    viewer_code = request.session.get("viewer_code")
    if not viewer_user_id or not viewer_code:
        return None
    user = get_user_by_id(viewer_user_id)
    if not user or user["share_code"] != viewer_code:
        return None
    return viewer_user_id


def viewer_display_overrides(request: Request, pilot, settings_dict: dict) -> dict:
    """Apply a viewer's own display preferences on top of the pilot's.

    Viewers can pick their own theme, clock format and link visibility; those
    live in cookies on their device and never touch the pilot's account.

    This exists as ONE function because the override used to be written
    inline in the tracker route and simply forgotten in the calendar route.
    A viewer on light mode therefore got a light tracker and a dark
    calendar — the same person, two pages, two themes. Anything rendering a
    page for a possibly-viewer must go through here.
    """
    out = dict(settings_dict)
    if pilot:
        return out
    cookie_tf = request.cookies.get("pt_viewer_tf")
    if cookie_tf in ("12", "24"):
        out["time_format"] = cookie_tf
    cookie_theme = request.cookies.get("pt_viewer_theme")
    if cookie_theme in ("dark", "light"):
        out["theme"] = cookie_theme
    if "pt_viewer_show_fa" in request.cookies:
        out["show_flightaware"] = request.cookies.get("pt_viewer_show_fa") == "1"
    if "pt_viewer_show_fr24" in request.cookies:
        out["show_fr24"] = request.cookies.get("pt_viewer_show_fr24") == "1"
    return out


def require_pilot(request: Request):
    """Returns the pilot user row, or a redirect response if not logged in
    as a pilot. Callers must check `isinstance(result, RedirectResponse)`."""
    pilot = current_pilot(request)
    if not pilot:
        return RedirectResponse(url="/login", status_code=303)
    return pilot


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------

def fmt_local(leg: FlightLeg, which: str = "dep", time_format: str = "24",
              with_zone: bool = True) -> str:
    if which == "dep":
        t = leg.dep_time_local
        info = leg.origin_info
    else:
        t = leg.arr_time_local
        info = leg.dest_info
    if time_format == "12":
        time_str = t.strftime("%I:%M %p").lstrip("0")
    else:
        time_str = t.strftime("%H:%M")
    if not info or not with_zone:
        return time_str
    # Was: a hard-coded sample of 2026-07-01, i.e. ALWAYS the summer label,
    # plus a fallback that rendered the city name ("Chicago") where a zone
    # belonged. Both are why labels looked inconsistent. leg.date is the
    # real date, so daylight time is answered for the day being shown.
    abbr = zone_label(info.timezone, leg.date)
    return f"{time_str} {abbr}" if abbr else time_str


def tz_abbr(leg: FlightLeg, which: str = "dep") -> Optional[str]:
    """The zone label on its own — "CT", "MT", "ET".

    fmt_local glues the zone onto the time and returns one string, which
    left templates no way to lay the two out separately. On a phone that
    string was long enough to wrap, so a departure read "7:00 AM" on one
    line and "CDT" on the next, twice per row.
    """
    info = leg.origin_info if which == "dep" else leg.dest_info
    if not info:
        return None
    return zone_label(info.timezone, leg.date)


def tracking_links(leg: FlightLeg) -> dict:
    cs = leg.callsign
    return {
        "fr24": f"https://www.flightradar24.com/{cs}",
        "flightaware": f"https://flightaware.com/live/flight/{cs}",
    }


def _fmt_utc_local(dt, tz_name, time_format="24"):
    """A UTC instant as a clock time at an airport."""
    if dt is None or not tz_name:
        return None
    try:
        local = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        return None
    text = (local.strftime("%I:%M %p").lstrip("0") if time_format == "12"
            else local.strftime("%H:%M"))
    label = zone_label(tz_name, dt)
    return f"{text} {label}".strip() if label else text


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except Exception:
        return None


# Shown instead of "Scheduled" on a leg whose arrival is this far past
# and which the poller never recorded anything for. Matches the 3-hour
# grace in get_current_info, so a leg stops being current and starts
# reading as untracked at the same instant rather than in two steps.
UNTRACKED_AFTER = timedelta(hours=3)
PHASE_UNTRACKED = "Not tracked"


def tag_index(user_id: int) -> dict:
    """{leg_id: (status_tag, phase_tag)} for this user, in one query.

    The flight lists render dozens of rows; reading each one's tags
    separately would be dozens of round trips for data that fits in a
    single SELECT.
    """
    from .db import get_connection
    conn = get_connection()
    try:
        return {r["id"]: (r["status_tag"], r["phase_tag"], bool(r["cancelled"]),
                          bool(r["closed"]))
                for r in conn.execute(
                    "SELECT f.id, f.status_tag, f.phase_tag, f.cancelled, f.closed "
                    "FROM roster r JOIN flights f ON f.id = r.flight_id "
                    "WHERE r.user_id = ?",
                    (user_id,))}
    finally:
        conn.close()


def time_index(user_id: int) -> dict:
    """{leg_id: row} of just the columns the strip's times need.

    Same reasoning as tag_index above, for the same lists: a row per query
    would be dozens of round trips. Deliberately a NARROW select — the
    flights table is wide, and a list row uses six timestamps and two
    flags. Legs with no flights record simply do not appear, and
    strip_lines treats a missing row as "nothing published yet".

    `closed_at` rides along for the settle rule (see settled_out), not for
    the strip. It is here rather than in its own query because this one
    already visits the same rows for the same list, and a second SELECT to
    fetch one more column off the same table is a round trip bought for
    nothing.
    """
    from .db import get_connection
    conn = get_connection()
    try:
        return {r["id"]: r for r in conn.execute(
            "SELECT f.id, f.out_actual_api, f.out_observed, f.out_estimated, "
            "       f.in_actual_api, f.in_observed, f.in_estimated, "
            "       f.cancelled, f.closed, f.closed_at "
            "FROM roster r JOIN flights f ON f.id = r.flight_id "
            "WHERE r.user_id = ?", (user_id,))}
    finally:
        conn.close()


def trip_slices(all_legs: list) -> list:
    """The roster cut into trips, in order.

    A trip begins at a leg carrying `trip_start`, which the parser sets
    from a blank line in the pasted schedule. Anything before the first
    such leg is its own trip, so a roster with NO markers at all comes
    back as one trip containing everything — which degrades to exactly the
    old behaviour rather than to an empty tracker.
    """
    trips, current = [], []
    for leg in all_legs:
        if leg.trip_start and current:
            trips.append(current)
            current = []
        current.append(leg)
    if current:
        trips.append(current)
    return trips


def tracker_window(all_legs: list, anchor_id: Optional[str]) -> Optional[set]:
    """Which legs the TRACKER shows: the anchor's trip. ONE trip.

    It used to keep the NEXT trip as well, on the reasoning that the page
    answers two questions — where is he now, and when does he go again.
    It does not. Appending the next trip put a second "Day 1 - August 28"
    under the first trip's last overnight, so the list read as one
    unbroken run of days that silently restarted its numbering, and the
    only thing separating a leg he is flying tonight from one two weeks
    out was a dashed line most people never saw. "When does he go again"
    is a question about a date, and the calendar answers it.

    The handover between trips is a matter of WHICH LEG anchors this
    window, not of how many trips it holds — see tracker_anchor.

    Returning None means "no opinion, show everything" — used when the
    anchor cannot be placed, so a bug here degrades to the old behaviour
    instead of to a blank page.
    """
    if not anchor_id:
        return None
    for trip in trip_slices(all_legs):
        if any(l.id == anchor_id for l in trip):
            return {l.id for l in trip}
    return None


# How long a finished trip stays on the tracker before the next one takes
# it over. TEN HOURS BECAUSE FAR 117 SAYS TEN HOURS: that is the minimum
# rest between duty periods, so the next trip cannot legally begin inside
# this window. It is not a guess at how long feels right — it is the
# shortest gap the regulation permits, which makes it the longest a
# finished trip can be held without ever hiding the next one.
TRIP_HANDOVER = timedelta(hours=10)


def tracker_anchor(info, now: datetime):
    """The leg that decides WHICH TRIP the tracker is showing.

    Three rules, in order:

      1. A leg is live -> that leg. Nothing competes with this.
      2. The last one landed less than TRIP_HANDOVER ago -> that leg.
         This is the whole reason the rule exists. Without it the trip
         disappears the instant the final leg goes past, and someone
         opening the app while he is still in the crew van is shown a
         trip in three weeks' time with no sign the one that just
         finished ever happened.
      3. Otherwise the next leg he flies.

    Rule 2 is NOT capped at the next departure, and the ten hours is not
    an arbitrary round number. FAR 117 requires a minimum of ten hours'
    rest between duty periods, so a legal schedule cannot put the next
    departure inside this window — and the report time before it puts the
    real gap comfortably wider still. The window is sized to the rule the
    pilot actually lives under.

    A cap was written and then removed: it could only ever fire on an
    illegal or mis-imported schedule, and rule 1 already covers that case
    anyway, since the leg goes live twenty minutes before it pushes and
    live beats everything.

    Returns None on an empty schedule, which the caller reads as "no
    opinion" and shows everything.
    """
    if info.current:
        return info.current

    landed = [l for l in info.past if l.arr_datetime_utc()]
    latest = max(landed, key=lambda l: l.arr_datetime_utc()) if landed else None

    if latest is not None and now < latest.arr_datetime_utc() + TRIP_HANDOVER:
        return latest

    # Falls through to the newest thing behind us if there is no schedule
    # ahead, so a roster that has entirely run out still renders its last
    # trip rather than an empty page.
    return (info.upcoming[0] if info.upcoming else None) or latest \
        or (info.past[-1] if info.past else None)


def _route_nm(leg) -> Optional[int]:
    """Great-circle distance between the two airports, whole nautical miles.

    A property of the ROUTE, not of the flight — the same number before
    pushback, in the cruise and after the leg closed. That is what makes it
    safe to show beside the live figures that invariant 9 blanks when there
    is no position fix: it cannot be mistaken for one, because it never
    moves.
    """
    oi, di = leg.origin_info, leg.dest_info
    if not oi or not di:
        return None
    if None in (oi.lat, oi.lon, di.lat, di.lon):
        return None
    try:
        return int(round(haversine_nm(oi.lat, oi.lon, di.lat, di.lon)))
    except Exception:
        return None


def _block_time(leg) -> Optional[str]:
    """Scheduled block time, "1h 22m".

    Taken from the two RESOLVED INSTANTS, never by subtracting one wall
    clock from the other. Those two clocks can be in different zones, and
    subtracting them directly is the bug that lost a whole day on an
    ANC-NRT leg in 1.1.0 — the same mistake in a different place. Going
    through the UTC instants means a leg crossing a zone, or a DST
    boundary, gets the block time actually flown.
    """
    dep, arr = leg.dep_datetime_utc(), leg.arr_datetime_utc()
    if not dep or not arr:
        return None
    minutes = int(round((arr - dep).total_seconds() / 60))
    if minutes <= 0:
        return None
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def leg_view(leg: Optional[FlightLeg], now: datetime, time_format: str = "24",
             tag_lookup: Optional[dict] = None,
             times_by_leg: Optional[dict] = None) -> Optional[dict]:
    if not leg:
        return None
    # Tags come from the row the poller wrote, never from the clock. The
    # old status_at() guessed a phase from scheduled times, which is
    # exactly the guessing v4.3 removed from the live card but left in
    # place on the flight lists.
    status_tag, phase_tag = None, tags.PHASE_SCHEDULED
    if tag_lookup is not None:
        status_tag, stored_phase, cancelled, closed = tag_lookup.get(
            leg.id, (None, None, False, False))
        # A leg the poller hasn't reached yet has no stored phase. It still
        # reads Scheduled, matching what view.build sends on the first
        # refresh — otherwise the card renders with no phase pill and then
        # grows one a few seconds later.
        phase_tag = stored_phase or tags.PHASE_SCHEDULED
        # ...but a flight that left three hours ago is not "Scheduled".
        # A leg imported after it was flown, or one that fell in a window
        # when the poller was down, has no stored phase and never will —
        # nothing sweeps a leg once it is past. Saying Scheduled there is
        # the app stating something it knows to be false. "Not tracked"
        # says the true thing: we have no record of this one.
        arr = leg.arr_datetime_utc()
        if (not closed and arr and now > arr + UNTRACKED_AFTER
                and phase_tag == tags.PHASE_SCHEDULED):
            phase_tag = PHASE_UNTRACKED
        if cancelled or status_tag == tags.STATUS_CANCELLED:
            phase_tag = None
    oi, di = leg.origin_info, leg.dest_info
    dep_line = arr_line = None
    if times_by_leg is not None:
        cancelled_f = closed_f = False
        if tag_lookup is not None:
            _s, _p, cancelled_f, closed_f = tag_lookup.get(
                leg.id, (None, None, False, False))
        dep_line, arr_line = flight_view.strip_lines(
            leg, times_by_leg.get(leg.id), phase_tag, cancelled_f, closed_f,
            time_format)
    return {
        "id": leg.id,
        "callsign": leg.callsign,
        "origin": leg.origin,
        "destination": leg.destination,
        "dep": fmt_local(leg, "dep", time_format),
        "arr": fmt_local(leg, "arr", time_format),
        # Zone codes are dropped on the collapsed card — they repeat on
        # every single time and were the main source of clutter and line
        # wrapping on a phone. The footer already says times are local to
        # each airport, and the expanded detail carries the full form.
        "dep_short": fmt_local(leg, "dep", time_format, with_zone=False),
        "arr_short": fmt_local(leg, "arr", time_format, with_zone=False),
        # The zone on its own, so a template can place it instead of being
        # handed "7:00 AM CDT" as one blob it has to wrap. A leg that
        # starts and ends in the same zone says it once; a leg that crosses
        # one says it twice, which is exactly when it matters. See
        # same_zone below.
        "dep_zone": tz_abbr(leg, "dep"),
        "arr_zone": tz_abbr(leg, "arr"),
        "same_zone": tz_abbr(leg, "dep") == tz_abbr(leg, "arr"),
        "status_tag": status_tag,
        "phase_tag": phase_tag,
        # One word for anywhere that still shows a single badge: the more
        # urgent of the two, which is what the old single pill conveyed.
        "status": status_tag or phase_tag,
        "links": tracking_links(leg),
        "date": str(leg.date),
        "is_deadhead": leg.is_deadhead,
        "origin_lat": oi.lat if oi else None,
        "origin_lon": oi.lon if oi else None,
        "dest_lat": di.lat if di else None,
        "dest_lon": di.lon if di else None,
        "origin_city": oi.city if oi else leg.origin,
        "dest_city": di.city if di else leg.destination,
        # The strip's two times, WITH state, so a list row and the card
        # above it cannot disagree about whether a flight is late. Absent
        # unless the caller supplied a bulk time index — the import pages
        # do not need them and should not pay for them. The calendar DOES
        # ask for them as of 1.18.0: it is the history browser, and
        # without these it printed the schedule back at you.
        "dep_line": dep_line,
        "arr_line": arr_line,
        # The airport's own NAME, for the expanded view's per-airport
        # blocks. Not a duplicate of the city: the strip above already
        # says "Dallas-Fort Worth to Oklahoma City", and what a person
        # driving to collect somebody still needs to know is WHICH field.
        "origin_name": oi.name if oi else None,
        "dest_name": di.name if di else None,
        # Two facts about the ROUTE, for the divider between the blocks.
        #
        # Neither is a measurement of the aeroplane and neither may ever be
        # mistaken for one — invariant 9 governs the live figures (percent
        # en route, distance to go, ETE) and those still require a position
        # fix. These two are properties of the SCHEDULE and the MAP: the
        # great-circle distance between two fixed points, and the block
        # time the bid line allows. They are equally true before pushback
        # and after landing, which is precisely why they are safe to print
        # when the live figures are blank.
        "route_nm": _route_nm(leg),
        "block_time": _block_time(leg),
    }


def _assign_trip_day_numbers(all_legs: list) -> dict:
    """Walks the FULL chronological schedule once and assigns each calendar
    date a trip-relative day number, resetting at trip boundaries. Computed
    over the whole schedule (not separately per past/upcoming) so a trip
    that's partly flown and partly still ahead numbers continuously across
    that split instead of incorrectly resetting to Day 1 mid-trip."""
    numbers = {}
    current_date = None
    day_trip_start = False
    trip_day_num = 0
    for leg in all_legs:
        if leg.date != current_date:
            if current_date is not None:
                trip_day_num = 1 if day_trip_start else trip_day_num + 1
                numbers[current_date] = trip_day_num
            current_date = leg.date
            day_trip_start = leg.trip_start
        elif leg.trip_start:
            day_trip_start = True
    if current_date is not None:
        trip_day_num = 1 if day_trip_start else trip_day_num + 1
        numbers[current_date] = trip_day_num
    return numbers


# Shorter than this and it is a turn, not a layover — even if it happens
# to straddle midnight. Long enough to leave the airport and sleep.
MIN_LAYOVER_SECONDS = 3 * 3600


def _day_buckets(legs: list) -> list:
    """Group a leg list into calendar-day buckets, in order."""
    buckets, current_date = [], None
    for leg in legs:
        if leg.date != current_date:
            buckets.append({"date": leg.date, "legs": [leg], "trip_start": leg.trip_start})
            current_date = leg.date
        else:
            buckets[-1]["legs"].append(leg)
            if leg.trip_start:
                buckets[-1]["trip_start"] = True
    return buckets


def overnight_index(all_legs: list) -> dict:
    """{date: {"city", "duration", "nights"}} for the WHOLE schedule.

    Computed over every leg the pilot has, not per list, because the
    tracker renders past and upcoming through two separate calls to
    group_legs_by_day. A layover that straddles that boundary — yesterday's
    arrival in `past`, tomorrow's departure in `upcoming` — had a bucket on
    each side and a neighbour on neither, so it silently showed nothing.
    That is the LFT case: in on the 9th, out on the 11th, and on the 10th
    the one number the family wants is how long he's actually there.

    Duty-day definition, unchanged: duty ends 15 minutes after block-in and
    starts 45 minutes before block-out. Nothing is shown across a trip
    boundary — that gap is time off, not a layover.
    """
    out = {}
    buckets = _day_buckets(all_legs)
    for i, bucket in enumerate(buckets[:-1]):
        nxt = buckets[i + 1]
        if nxt["trip_start"]:
            continue
        last_leg, next_leg = bucket["legs"][-1], nxt["legs"][0]
        last_arr, next_dep = last_leg.arr_datetime_utc(), next_leg.dep_datetime_utc()
        if not last_arr or not next_dep:
            continue
        gap = (next_dep - timedelta(minutes=45)) - (last_arr + timedelta(minutes=15))
        secs = gap.total_seconds()
        # Bounded at BOTH ends, because a raw "gap between two flying days"
        # produces nonsense on either side of a real layover:
        #
        #   too short — a turn that happens to cross midnight showed as
        #   "Overnight in Waco — 0h 42m", which is a 42-minute sit, not a
        #   hotel.
        #
        #   too long — a gap between trips is days off at home. Blank lines
        #   in the paste normally mark those, but a pilot who pastes one
        #   unbroken block has no boundaries at all, and every such gap
        #   became "4 nights in Dallas-Fort Worth — 98h 38m". The ceiling
        #   is the same figure the import-review page uses to SUGGEST a
        #   trip break, so the two agree by construction.
        if secs < MIN_LAYOVER_SECONDS or secs > GAP_TRIP_THRESHOLD_HOURS * 3600:
            continue
        hours, minutes = int(secs // 3600), int((secs % 3600) // 60)
        # A 33-hour layover is two nights, not one, and calling it an
        # overnight reads as a mistake. Count the calendar dates actually
        # spent away rather than dividing by 24 — in at 23:50 and out at
        # 06:00 next morning is one night, not two.
        #
        # In the LAYOVER AIRPORT'S local time, not UTC. An evening arrival
        # in the US is already tomorrow in UTC, so counting UTC dates
        # reported the real 33-hour LFT layover as a single night.
        tz = None
        if last_leg.dest_info and last_leg.dest_info.timezone:
            try:
                tz = ZoneInfo(last_leg.dest_info.timezone)
            except Exception:
                tz = None
        if tz is not None:
            nights = (next_dep.astimezone(tz).date()
                      - last_arr.astimezone(tz).date()).days
        else:
            nights = round(secs / 86400)
        nights = max(1, nights)
        out[bucket["date"]] = {
            "duration": f"{hours}h {minutes:02d}m",
            "city": (last_leg.dest_info.city if last_leg.dest_info
                     else last_leg.destination),
            "nights": nights,
        }
    return out


def group_legs_by_day(legs: list, day_numbers: dict, now: datetime, time_format: str = "24",
                      tags_by_leg: Optional[dict] = None,
                      overnights: Optional[dict] = None,
                      times_by_leg: Optional[dict] = None) -> list:
    """Groups legs by calendar date, labeled 'Day N - March 27' where N
    resets to 1 at each trip boundary (a blank line in the pasted FFDO —
    see parser.py). Trip boundaries are explicit and pilot-controlled, not
    guessed from gap length, so a real 30+ hour layover mid-trip still
    shows correctly while a multi-day gap *between* two separate trips
    (e.g. days off at home) doesn't get mislabeled as one.

    `overnights` comes from overnight_index(info.all_legs) — computed once
    over the WHOLE schedule, not per list, so a layover straddling the
    past/upcoming boundary still gets its label. Same reasoning as
    day_numbers.
    """
    if not legs:
        return []

    overnights = overnights or {}
    groups = []
    for bucket in _day_buckets(legs):
        trip_day_num = day_numbers.get(bucket["date"], 1)
        date_label = bucket["date"].strftime("%B %d").replace(" 0", " ")
        groups.append({
            "date_label": f"Day {trip_day_num} - {date_label}",
            "legs": [leg_view(l, now, time_format, tags_by_leg, times_by_leg)
                     for l in bucket["legs"]],
            "overnight": overnights.get(bucket["date"]),
            "trip_start": bucket["trip_start"],
        })
    return groups


GAP_TRIP_THRESHOLD_HOURS = 35.0


def apply_gap_trip_starts(legs: list, threshold_hours: float = GAP_TRIP_THRESHOLD_HOURS) -> None:
    """Mutates legs in place: suggests trip_start=True on the first leg of
    any flying day where the duty-day gap since the previous flying day is
    >= threshold_hours. This is only ever a starting guess shown on the
    import review page — the pilot confirms or adjusts every suggestion
    before anything is saved. Explicit blank-line trip_start values from
    the parser are left as-is (this only ever adds suggestions, never
    removes one the pilot's paste already marked)."""
    day_buckets = []
    current_date = None
    for leg in legs:
        if leg.date != current_date:
            day_buckets.append([leg])
            current_date = leg.date
        else:
            day_buckets[-1].append(leg)

    for i in range(1, len(day_buckets)):
        prev_last = day_buckets[i - 1][-1]
        this_first = day_buckets[i][0]
        last_arr = prev_last.arr_datetime_utc()
        this_dep = this_first.dep_datetime_utc()
        if not last_arr or not this_dep:
            continue
        duty_ends = last_arr + timedelta(minutes=15)
        duty_starts = this_dep - timedelta(minutes=45)
        gap_hours = (duty_starts - duty_ends).total_seconds() / 3600
        if gap_hours >= threshold_hours:
            this_first.trip_start = True


def build_review_legs(legs: list, time_format: str = "24") -> list:
    """Flat, chronological view of a freshly-parsed (not yet saved)
    schedule for the drag-and-drop import review page. Each leg carries
    its raw fields as hidden-input-ready strings so the confirm step can
    rebuild the FlightLeg objects without re-parsing the original text,
    plus whether a trip break is suggested immediately before it."""
    out = []
    for i, leg in enumerate(legs):
        out.append({
            "raw_date": leg.date.isoformat(),
            "raw_flight": leg.flight_number,
            "raw_origin": leg.origin,
            "raw_dest": leg.destination,
            "raw_dep": leg.dep_time_local.isoformat(),
            "raw_arr": leg.arr_time_local.isoformat(),
            "raw_dh": "1" if leg.is_deadhead else "0",
            "callsign": leg.callsign,
            "route": f"{leg.origin} → {leg.destination}",
            "date_label": leg.date.strftime("%B %d").replace(" 0", " "),
            "dep": fmt_local(leg, "dep", time_format, with_zone=False),
            "arr": fmt_local(leg, "arr", time_format, with_zone=False),
            "dep_zone": tz_abbr(leg, "dep"),
            "arr_zone": tz_abbr(leg, "arr"),
            "same_zone": tz_abbr(leg, "dep") == tz_abbr(leg, "arr"),
            "is_deadhead": leg.is_deadhead,
            "suggested_break_before": bool(leg.trip_start and i > 0),
        })
    return out


def build_diff_rows(entries: list, time_format: str = "24") -> list:
    """Render one section of the import diff. (N1, 1.5.0)

    A "changed" entry carries the OLD times as well as the new ones,
    because "3729 DFW→OKC changed" tells the pilot nothing he can act on,
    and the whole reason the diff exists is to be actionable.
    """
    out = []
    for entry in entries:
        leg, was = entry["leg"], entry.get("was")
        row = {
            "id": flight_key(leg.id),
            "callsign": leg.callsign,
            "route": f"{leg.origin} → {leg.destination}",
            "date_label": leg.date.strftime("%b %d").replace(" 0", " "),
            "dep": fmt_local(leg, "dep", time_format, with_zone=False),
            "arr": fmt_local(leg, "arr", time_format, with_zone=False),
            "dep_zone": tz_abbr(leg, "dep"),
            "arr_zone": tz_abbr(leg, "arr"),
            "same_zone": tz_abbr(leg, "dep") == tz_abbr(leg, "arr"),
            "is_deadhead": leg.is_deadhead,
            # Whether this leg has already been flown. The review page
            # holds flown removals back from the default tick (1.20.0) —
            # see the removed section there for why that distinction is
            # the whole safety mechanism.
            "flown": bool(entry.get("flown")),
            # A flown leg whose only difference is the deadhead flag. Its
            # times are settled and are not being restated.
            "dh_only": bool(entry.get("dh_only")),
            "was": None,
        }
        if was is not None:
            row["was"] = {
                "dep": fmt_local(was, "dep", time_format, with_zone=False),
                "arr": fmt_local(was, "arr", time_format, with_zone=False),
                "is_deadhead": was.is_deadhead,
            }
        out.append(row)
    return out


def build_trip_spans(legs: list, time_format: str = "24") -> list:
    """Groups legs into trips (using the same trip_start boundaries as
    everywhere else) and returns each trip's date range + start/finish
    times, for the calendar's continuous working-day bar."""
    trips = []
    current = None
    for leg in legs:
        if current is None or leg.trip_start:
            if current:
                trips.append(current)
            current = {"start_date": leg.date, "end_date": leg.date, "legs": [leg]}
        else:
            current["end_date"] = leg.date
            current["legs"].append(leg)
    if current:
        trips.append(current)

    out = []
    for trip in trips:
        first_leg = trip["legs"][0]
        last_leg = trip["legs"][-1]
        out.append({
            "start_date": trip["start_date"],
            "end_date": trip["end_date"],
            "start_time": fmt_local(first_leg, "dep", time_format),
            "finish_time": fmt_local(last_leg, "arr", time_format),
        })
    return out


# ---------------------------------------------------------------------------
# Setup (first-run bootstrap) + login/logout
# ---------------------------------------------------------------------------

@app.get("/setup", response_class=HTMLResponse)
async def setup_get(request: Request):
    if count_users() > 0:
        return RedirectResponse(url="/login", status_code=303)
    template = jinja_env.get_template("setup.html")
    return HTMLResponse(template.render(request=request, error=None))


@app.post("/setup", response_class=HTMLResponse)
async def setup_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    email: str = Form(""),
):
    if count_users() > 0:
        return RedirectResponse(url="/login", status_code=303)

    username = username.strip()
    error = None
    if len(username) < 3:
        error = "Username must be at least 3 characters."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif password != confirm_password:
        error = "Passwords don't match."

    if error:
        template = jinja_env.get_template("setup.html")
        return HTMLResponse(template.render(request=request, error=error))

    user_id = create_user(username, password, email)
    request.session["user_id"] = user_id
    code = set_recovery_code(user_id)
    request.session["_pending_recovery_code"] = code
    request.session["_pending_recovery_next"] = "/admin"
    return RedirectResponse(url="/recovery-code", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if count_users() == 0:
        return RedirectResponse(url="/setup", status_code=303)
    template = jinja_env.get_template("login.html")
    return HTMLResponse(template.render(request=request, error=None))


@app.get("/register", response_class=HTMLResponse)
async def register_get(request: Request):
    template = jinja_env.get_template("register.html")
    return HTMLResponse(template.render(request=request, error=None))


@app.post("/register", response_class=HTMLResponse)
async def register_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    email: str = Form(""),
):
    if not check_rate_limit(request, "register", max_attempts=5, window_seconds=3600):
        template = jinja_env.get_template("register.html")
        return HTMLResponse(template.render(request=request, error="Too many attempts. Try again in a bit."), status_code=429)

    username = username.strip()
    error = None
    if len(username) < 3:
        error = "Username must be at least 3 characters."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif password != confirm_password:
        error = "Passwords don't match."
    elif get_user_by_username(username):
        error = "That username is already taken."

    if error:
        template = jinja_env.get_template("register.html")
        return HTMLResponse(template.render(request=request, error=error))

    user_id = create_user(username, password, email)
    request.session["user_id"] = user_id
    request.session.pop("viewer_user_id", None)
    request.session.pop("viewer_code", None)
    code = set_recovery_code(user_id)
    request.session["_pending_recovery_code"] = code
    request.session["_pending_recovery_next"] = "/admin"
    return RedirectResponse(url="/recovery-code", status_code=303)


@app.post("/login/pilot", response_class=HTMLResponse)
async def login_pilot(request: Request, username: str = Form(...), password: str = Form(...)):
    if not check_rate_limit(request, "login_pilot", max_attempts=8, window_seconds=600):
        template = jinja_env.get_template("login.html")
        return HTMLResponse(template.render(request=request, username=username,
                                           error="Too many attempts. Try again in a few minutes."),
                            status_code=429)
    user = get_user_by_username(username)
    if not user or not verify_password(password, user["password_hash"]):
        template = jinja_env.get_template("login.html")
        # Hand the username back so a mistyped password doesn't cost you the
        # whole form. The password is deliberately NOT echoed: it would end
        # up in the page source, browser cache and any proxy log, and the
        # browser's own password manager refills it anyway.
        return HTMLResponse(template.render(request=request, username=username,
                                           error="Incorrect username or password."))
    request.session["user_id"] = user["id"]
    request.session.pop("viewer_user_id", None)
    request.session.pop("viewer_code", None)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/login/code", response_class=HTMLResponse)
async def login_code(request: Request, code: str = Form(...)):
    if not check_rate_limit(request, "login_code", max_attempts=15, window_seconds=600):
        template = jinja_env.get_template("login.html")
        return HTMLResponse(template.render(request=request, error="Too many attempts. Try again in a few minutes."), status_code=429)
    user = get_user_by_share_code(code.strip())
    if not user:
        template = jinja_env.get_template("login.html")
        return HTMLResponse(template.render(request=request, error="That tracking code doesn't match anyone."))
    request.session["viewer_user_id"] = user["id"]
    request.session["viewer_code"] = user["share_code"]
    request.session.pop("user_id", None)
    return RedirectResponse(url="/", status_code=303)


@app.get("/recovery-code", response_class=HTMLResponse)
async def recovery_code_reveal(request: Request):
    code = request.session.pop("_pending_recovery_code", None)
    next_url = request.session.pop("_pending_recovery_next", "/admin")
    if not code:
        # Nothing pending (e.g. page revisited/bookmarked after the fact) —
        # there's no code to show a second time, so just move along.
        return RedirectResponse(url="/admin", status_code=303)
    template = jinja_env.get_template("recovery_code.html")
    return HTMLResponse(template.render(request=request, recovery_code=code, next_url=next_url))


@app.get("/login/forgot", response_class=HTMLResponse)
async def forgot_password_get(request: Request):
    template = jinja_env.get_template("forgot_password.html")
    return HTMLResponse(template.render(request=request, error=None))


@app.post("/login/forgot", response_class=HTMLResponse)
async def forgot_password_post(
    request: Request,
    username: str = Form(...),
    recovery_code: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    if not check_rate_limit(request, "forgot_password", max_attempts=8, window_seconds=600):
        template = jinja_env.get_template("forgot_password.html")
        return HTMLResponse(template.render(request=request, error="Too many attempts. Try again in a few minutes."), status_code=429)

    error = None
    if len(new_password) < 8:
        error = "New password must be at least 8 characters."
    elif new_password != confirm_password:
        error = "Passwords don't match."
    elif not reset_password_with_recovery_code(username.strip(), recovery_code, new_password):
        error = "That username/recovery code combination doesn't match."

    if error:
        template = jinja_env.get_template("forgot_password.html")
        return HTMLResponse(template.render(request=request, error=error))

    # The recovery code just used is now spent — rotate to a new one and
    # show it, same as at registration.
    user = get_user_by_username(username.strip())
    new_code = set_recovery_code(user["id"])
    request.session["_pending_recovery_code"] = new_code
    request.session["_pending_recovery_next"] = "/login"
    return RedirectResponse(url="/recovery-code", status_code=303)


@app.post("/settings/regenerate-recovery")
async def settings_regenerate_recovery(request: Request):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    new_code = set_recovery_code(pilot["id"])
    request.session["_pending_recovery_code"] = new_code
    request.session["_pending_recovery_next"] = "/settings"
    return RedirectResponse(url="/recovery-code", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# ---------------------------------------------------------------------------
# Tracker (viewer) — pilot or valid share-code session
# ---------------------------------------------------------------------------

def resolve_selected_leg(info, leg_id: Optional[str], now: Optional[datetime] = None):
    """Which flight is the map/collapsed card showing? Default: whatever
    tracker_anchor picks, which is the same leg that decides which trip
    the LIST is scoped to. A leg_id (from tapping a flight in the list)
    overrides that, as long as it's a real leg on this schedule.

    Sharing the anchor is not a tidiness argument. These two used to
    compute their default separately from the same three fallbacks, which
    agreed until the 10-hour handover was added on one side only — at
    which point the list could be showing the trip that landed an hour
    ago while the card above it showed the first leg of one a fortnight
    out, and tapping the card's leg would have selected a flight the list
    does not contain.
    """
    if now is None:
        now = datetime.now(ZoneInfo("UTC"))
    selected_leg = tracker_anchor(info, now)
    if leg_id:
        match = next((l for l in info.all_legs if l.id == leg_id), None)
        if match:
            selected_leg = match
    is_selected_live = bool(selected_leg and info.current and selected_leg.id == info.current.id)
    return selected_leg, is_selected_live


def compute_live_payload(user_id: int, selected_leg, is_selected_live: bool,
                         now: datetime, poll_seconds: int, time_format: str = "24"):
    """Everything the card shows, READ FROM THE FLIGHT ROW.

    In v4 this function fetched live ADS-B, wrote track points, advanced
    the aircraft state machine and then reconciled three tables to produce
    a status — on every page render, for every viewer. Two engines ran the
    same logic on different clocks and whichever got there first changed
    the answer.

    Now it reads. The poller decided, wrote it down, and this renders it.
    Nothing here fetches, spends a query, or writes, so a family member
    refreshing fifty times during a delay costs nothing and can't move the
    flight's state.

    `is_selected_live` no longer gates anything meaningful — a past leg's
    stored times, gates and closeout record are just as much a part of the
    row as a live one's position — but it is kept in the signature because
    callers pass it and the template still distinguishes the two.
    """
    if not selected_leg:
        return None, {"progress_pct": None, "ete": None, "distance_nm": None,
                      "breadcrumb": [], "aircraft": None, "status": None,
                      "phase_tag": None, "status_tag": None}
    row = get_flight(selected_leg.id)
    payload = flight_view.build(row, selected_leg, now, time_format)
    live = payload.pop("live", None)
    return live, payload


# How long a closed leg stays in the list after it closes out. Half an
# hour is roughly the walk off the aeroplane and down the concourse: long
# enough that someone who got the landing notification and reached for
# their phone still finds the flight they came to look at, short enough
# that a four-leg day does not end as four rows about the past and one
# about the present.
LEG_SETTLE = timedelta(minutes=30)


def settled_out(leg_ids: list, times_by_leg: Optional[dict], now: datetime) -> set:
    """Which legs have finished settling and should leave the list.

    A leg drops LEG_SETTLE after its CLOSEOUT, not after its scheduled
    arrival. Those are different instants and the difference is the whole
    point: closeout is the app concluding the flight is genuinely over
    (see CLOSURE), whereas a scheduled arrival is a guess that a two-hour
    delay makes a lie. Dropping on the schedule would clear a leg off the
    list while the aeroplane was still at altitude.

    A leg with no closeout NEVER drops, however old. The absence of a
    closeout means the app does not know how the flight ended, and
    quietly removing it would present that as resolved.

    THE LAST REMAINING LEG NEVER DROPS. If it did, the list would empty
    itself thirty minutes after the final landing and stay empty for the
    rest of the ten-hour handover — the exact window in which someone is
    most likely to open the app to check he got in. This is why the
    function takes the whole list rather than being asked leg by leg: "is
    this one still needed" cannot be answered without knowing what else
    is left, and a per-leg version of this rule would have emptied the
    page.
    """
    if not times_by_leg:
        return set()
    drop = set()
    for lid in leg_ids:
        row = times_by_leg.get(lid)
        if row is None or not row["closed"]:
            continue
        closed_at = _parse_iso(row["closed_at"] if "closed_at" in row.keys() else None)
        if closed_at is None:
            continue
        # A stored timestamp with no zone is UTC — that is what closure.py
        # writes. Comparing it to an aware `now` raises rather than
        # returning False, so this cannot be left to chance.
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=ZoneInfo("UTC"))
        if now - closed_at >= LEG_SETTLE:
            drop.add(lid)
    # Never empty the list. Give back the newest thing dropped rather than
    # picking an arbitrary survivor, so what remains is the leg he most
    # recently finished.
    if drop and len(drop) == len(leg_ids):
        drop.discard(leg_ids[-1])
    return drop


def build_flight_list(info, day_numbers: dict, now: datetime, time_format: str,
                      tags_by_leg, overnights: dict,
                      times_by_leg: Optional[dict] = None) -> list:
    """Past, current and upcoming as ONE chronological list of day groups.

    Previously the page built past and upcoming through two separate calls
    and left the current flight out of both, so the list had a hole exactly
    where the pilot is. Scrolling it gave no reference point: yesterday
    ended, and the next thing shown was tomorrow.

    Building one sequence also fixes two things that fell out of the split:
    a day holding both a flown leg AND the live leg produced two day-cards
    with the same "Day 3 - August 16" label, and a layover whose two ends
    landed on opposite sides of the split had no label at all.

    Each row carries `is_past` / `is_current`, and each group carries
    `all_past`, so the Show-past-flights toggle can hide a whole day or
    single rows inside a mixed day without the server rendering twice.
    """
    ordered = list(info.past)
    if info.current:
        ordered.append(info.current)
    ordered.extend(info.upcoming)

    past_ids = {l.id for l in info.past}
    current_id = info.current.id if info.current else None

    # SCOPED TO ONE TRIP (1.16.0; was this trip and the next since 1.11.0).
    # tracker_anchor picks which one, and the card above the list resolves
    # its default through the same call, so the two cannot end up on
    # different trips.
    #
    # Past legs of the CURRENT trip stay: "he has done three of today's
    # four" is the question this page exists to answer. Past legs of older
    # trips leave entirely; they are the calendar's job.
    anchor = tracker_anchor(info, now)
    window = tracker_window(info.all_legs, anchor.id if anchor else None)
    if window is not None:
        ordered = [l for l in ordered if l.id in window]

    # SETTLED LEGS LEAVE (1.17.0). Applied AFTER the trip window, not
    # before, so "the last remaining leg" means the last one of this trip
    # rather than the last on the roster. The live leg is never dropped
    # because a live leg is not closed.
    dropped = settled_out([l.id for l in ordered], times_by_leg, now)
    if dropped:
        ordered = [l for l in ordered if l.id not in dropped]

    groups = group_legs_by_day(ordered, day_numbers, now, time_format,
                               tags_by_leg, overnights, times_by_leg)
    for group in groups:
        for row in group["legs"]:
            row["is_past"] = row["id"] in past_ids
            row["is_current"] = row["id"] == current_id
        group["all_past"] = all(r["is_past"] for r in group["legs"])
        group["first_live"] = False
    # The scroll landmark goes before the first day that is not entirely
    # past, so every element the toggle reveals sits above it.
    for group in groups:
        if not group["all_past"]:
            group["first_live"] = True
            break
    else:
        # Everything is past — nothing follows the list to anchor against,
        # so the landmark goes nowhere and togglePast falls back to leaving
        # scroll alone.
        pass
    return groups


@app.get("/", response_class=HTMLResponse)
async def viewer(request: Request, leg: Optional[str] = None):
    pilot = current_pilot(request)
    viewer_uid = None if pilot else current_viewer_user_id(request)
    user_id = pilot["id"] if pilot else viewer_uid
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    settings = load_settings(user_id)
    info = get_current_info(user_id)
    now = datetime.now(ZoneInfo("UTC"))
    # ONE PLACE, not a second copy of it (1.19.0). This block used to
    # re-implement viewer_display_overrides inline, line for line —
    # cookie names, valid values and all. Which is how the CALENDAR ended
    # up honouring a viewer's theme and ignoring their clock: with the
    # rule written out in two routes, fixing one did not fix the other,
    # and the shared function existed the whole time.
    display = viewer_display_overrides(request, pilot, settings.model_dump())
    tf = display["time_format"]

    selected_leg, is_selected_live = resolve_selected_leg(info, leg, now)
    tags_by_leg = tag_index(user_id)
    times_by_leg = time_index(user_id)
    selected = leg_view(selected_leg, now, tf, tags_by_leg)
    live, extra = compute_live_payload(user_id, selected_leg, is_selected_live, now, settings.poll_seconds, tf)
    if selected:
        selected.update(extra)
    settings_dict = display
    day_numbers = _assign_trip_day_numbers(info.all_legs)
    # Over the WHOLE schedule, so a layover straddling the past/upcoming
    # split still gets a label. See overnight_index().
    overnights = overnight_index(info.all_legs)
    groups = build_flight_list(info, day_numbers, now, tf,
                               tags_by_leg, overnights, times_by_leg)
    ctx = {
        "request": request,
        "current": selected,
        "is_selected_live": is_selected_live,
        "live": live,
        "selected_id": selected_leg.id if selected_leg else None,
        # Lets the page show a way back to the active flight when the user
        # is looking at some other leg. The active flight isn't in the
        # upcoming/past lists, so without this there's no row to tap to
        # return to it.
        "current_leg_id": info.current.id if info.current else None,
        "flight_groups": groups,
        # EVERY LEG OF THE TRIP, as coordinate pairs, so the map can show
        # the shape of the whole trip behind the one leg being tracked.
        # Derived from the groups that are already built rather than
        # re-walking the roster: whatever the tracker is listing is exactly
        # what the map should outline, and computing the two separately is
        # how they come to disagree.
        "trip_routes": [
            [l["origin_lat"], l["origin_lon"], l["dest_lat"], l["dest_lon"]]
            for g in groups for l in g["legs"]
            if l.get("origin_lat") is not None and l.get("dest_lat") is not None
        ],
        # past_count is gone with the Show-past-flights button (1.11.0).
        # The tracker no longer holds every past leg to be revealed; it
        # holds this trip and the next, and every row in it is visible.
        "settings": settings_dict,
        "poll_ms": max(10, settings.poll_seconds) * 1000,
        "is_pilot": pilot is not None,
        "active_tab": "tracker",
    }
    template = jinja_env.get_template("viewer.html")
    return HTMLResponse(template.render(**ctx))


@app.get("/viewer-settings", response_class=HTMLResponse)
async def viewer_settings_get(request: Request):
    """Viewers get the SAME settings page as pilots, minus what they can't own.

    There used to be a second template, viewer_settings.html, holding a
    stripped copy of the display controls. Two files meant two chances to
    drift, and they already had: the pilot form named its checkbox
    show_flightaware while the viewer form called it show_fa. One template
    now, with the pilot-only sections behind {% if is_pilot %} — the API
    key, the poll interval, account recovery — and the admin roster behind
    is_admin on top of that.

    Storage still differs and should: a pilot's settings live in the
    database and follow their account, a viewer's live in a cookie on the
    device in front of them. Only the form's action changes.
    """
    pilot = current_pilot(request)
    viewer_uid = None if pilot else current_viewer_user_id(request)
    if not pilot and not viewer_uid:
        return RedirectResponse(url="/login", status_code=303)
    theme = request.cookies.get("pt_viewer_theme", "dark")
    tf = request.cookies.get("pt_viewer_tf", "24")
    if theme not in ("dark", "light"):
        theme = "dark"
    if tf not in ("12", "24"):
        tf = "24"
    view_settings = SimpleNamespace(
        theme=theme,
        time_format=tf,
        show_flightaware=request.cookies.get("pt_viewer_show_fa", "1") == "1",
        show_fr24=request.cookies.get("pt_viewer_show_fr24", "1") == "1",
    )
    template = jinja_env.get_template("settings.html")
    return HTMLResponse(template.render(
        request=request, s=view_settings, saved=False,
        is_pilot=False, is_admin=False, active_tab="settings",
        post_to="/viewer-settings",
    ))


@app.post("/viewer-settings")
async def viewer_settings_post(
    request: Request,
    theme: str = Form("dark"),
    time_format: str = Form("24"),
    # Renamed from show_fa to match the pilot form now that both post from
    # the same template. The COOKIE name is unchanged, so nobody's saved
    # preference resets on upgrade.
    show_flightaware: Optional[str] = Form(None),
    show_fr24: Optional[str] = Form(None),
):
    pilot = current_pilot(request)
    viewer_uid = None if pilot else current_viewer_user_id(request)
    if not pilot and not viewer_uid:
        return RedirectResponse(url="/login", status_code=303)
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie("pt_viewer_theme", "light" if theme == "light" else "dark", max_age=60 * 60 * 24 * 365)
    resp.set_cookie("pt_viewer_tf", "12" if time_format == "12" else "24", max_age=60 * 60 * 24 * 365)
    resp.set_cookie("pt_viewer_show_fa", "1" if show_flightaware is not None else "0", max_age=60 * 60 * 24 * 365)
    resp.set_cookie("pt_viewer_show_fr24", "1" if show_fr24 is not None else "0", max_age=60 * 60 * 24 * 365)
    return resp


# ---------------------------------------------------------------------------
# Calendar — pilot or viewer, same rules as the tracker page
# ---------------------------------------------------------------------------

@app.get("/calendar", response_class=HTMLResponse)
async def calendar_page(request: Request, month: Optional[str] = None):
    pilot = current_pilot(request)
    viewer_uid = None if pilot else current_viewer_user_id(request)
    user_id = pilot["id"] if pilot else viewer_uid
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    settings = load_settings(user_id)
    # THE VIEWER'S OWN DISPLAY PREFERENCES, RESOLVED ONCE, BEFORE ANYTHING
    # IS FORMATTED (1.19.0). This route already called
    # viewer_display_overrides — but only on its way OUT, to hand the
    # template a theme. Every time on the page had already been formatted
    # from `settings.time_format`, which is the PILOT's. So a viewer who
    # chose a 12-hour clock got a light calendar full of 24-hour times:
    # their theme honoured, their clock ignored, on the same page.
    #
    # That is the exact failure viewer_display_overrides was written for
    # in the first place — applied to the wrong half of the route. The
    # override now happens before it can be read wrong, and the display
    # settings are the ONLY ones used below.
    display = viewer_display_overrides(request, pilot, settings.model_dump())
    time_format = display["time_format"]
    now = datetime.now(ZoneInfo("UTC"))
    # An INSTANT is fine in UTC and compares correctly against anything.
    # A CALENDAR DAY is not: turning an instant into a date needs a zone,
    # and doing it in UTC meant "today" rolled over at 7pm Central. Every
    # evening the calendar highlighted tomorrow and the agenda scrolled to
    # the wrong day. astimezone() with no argument converts to the
    # container's local zone, which docker-compose already pins with
    # TZ (America/Chicago by default).
    today = now.astimezone().date()

    legs = load_schedule(user_id)
    tags_by_leg = tag_index(user_id)
    # THE CALENDAR NOW PAYS FOR THIS (1.18.0). It is the history browser,
    # and history is exactly what these columns hold — what a leg actually
    # did, as against what the bid line said it would. Without it the
    # agenda printed the schedule back at you and a leg that went two
    # hours late read identical to one that ran to the minute.
    #
    # ONE query for the whole month, not one per leg: see time_index. That
    # is what makes it affordable here, where a month can hold sixty legs.
    times_by_leg = time_index(user_id)
    by_date = {}
    for leg in legs:
        by_date.setdefault(leg.date, []).append(leg)
    trips = build_trip_spans(legs, time_format)

    def trip_for_day(d):
        for trip in trips:
            if trip["start_date"] <= d <= trip["end_date"]:
                return trip
        return None

    # ONE MONTH AT A TIME (1.5.0). This used to render every month that
    # had a flight, stacked down one page. That was fine while retention
    # was 30 days and the import replaced the roster — there was never
    # more than a month of it. With 365-day retention and N1's additive
    # import, the same code renders a whole YEAR of grids and agendas into
    # a single document, on a phone.
    #
    # The month shown is a URL parameter, so it survives a refresh, can be
    # linked, and is what the browser Back button steps through.
    months_with_data = sorted({(l.date.year, l.date.month) for l in legs})
    if not months_with_data:
        months_with_data = [(today.year, today.month)]

    def _key(y, m):
        return f"{y:04d}-{m:02d}"

    available = [_key(y, m) for y, m in months_with_data]
    # Default to the month being LIVED IN if there is anything in it,
    # otherwise the nearest month that has flights — landing a crew member
    # on an empty January because it happens to sort first is exactly the
    # kind of thing that reads as broken.
    active = month if month in available else None
    if active is None:
        this_month = _key(today.year, today.month)
        if this_month in available:
            active = this_month
        else:
            future = [k for k in available if k >= this_month]
            active = future[0] if future else available[-1]
    idx = available.index(active)
    prev_month = available[idx - 1] if idx > 0 else None
    next_month = available[idx + 1] if idx < len(available) - 1 else None

    cal = cal_module.Calendar(firstweekday=6)  # weeks start Sunday
    month_blocks = []
    for year, month in [(int(active[:4]), int(active[5:7]))]:
        weeks = []
        week = []
        for d in cal.itermonthdates(year, month):
            day_legs = by_date.get(d, [])
            trip = trip_for_day(d)
            weekday = d.weekday()  # Monday=0 ... Sunday=6
            is_first_col = weekday == 6  # Sunday
            is_last_col = weekday == 5   # Saturday
            week.append({
                "date": d,
                "iso": d.isoformat(),
                "day": d.day,
                "in_month": d.month == month,
                "is_today": d == today,
                "in_trip": trip is not None,
                "round_left": bool(trip and (d == trip["start_date"] or is_first_col)),
                "round_right": bool(trip and (d == trip["end_date"] or is_last_col)),
                "start_time": trip["start_time"] if trip and d == trip["start_date"] else None,
                "finish_time": trip["finish_time"] if trip and d == trip["end_date"] else None,
                "leg_count": len(day_legs),
            })
            if len(week) == 7:
                weeks.append(week)
                week = []

        _, last_day = cal_module.monthrange(year, month)
        agenda = []
        for day_num in range(1, last_day + 1):
            d = date(year, month, day_num)
            day_legs = by_date.get(d, [])
            trip = trip_for_day(d)
            agenda.append({
                "iso": d.isoformat(),
                "label": d.strftime("%A, %B %d").replace(" 0", " "),
                "is_today": d == today,
                "legs": [leg_view(l, now, time_format, tags_by_leg,
                                  times_by_leg)
                         for l in day_legs],
                "in_trip": trip is not None,
                "trip_is_start": bool(trip and d == trip["start_date"]),
                "trip_is_end": bool(trip and d == trip["end_date"]),
                # Only butt this card seamlessly against the next one if we're
                # sure that next card is still in this same month's list —
                # a trip crossing a month boundary just gets a normal gap
                # there instead of risking a broken-looking seam.
                "seamless_after": bool(trip and d != trip["end_date"] and day_num < last_day),
            })

        month_blocks.append({
            "label": date(year, month, 1).strftime("%B %Y"),
            "weeks": weeks,
            "agenda": agenda,
        })

    template = jinja_env.get_template("calendar.html")
    return HTMLResponse(template.render(
        request=request,
        month_blocks=month_blocks,
        active_month=active,
        prev_month=prev_month,
        next_month=next_month,
        active_tab="calendar",
        month_choices=[{"value": k,
                        "label": datetime.strptime(k, "%Y-%m").strftime("%B %Y")}
                       for k in reversed(available)],
        settings=display,
        is_pilot=pilot is not None,
    ))


# ---------------------------------------------------------------------------
# Admin (schedule) — pilot only
# ---------------------------------------------------------------------------

@app.post("/admin/diagnostics/endpoints")
async def admin_diagnostics_save(request: Request):
    """Save the ADS-B feed list edited on the diagnostics page.

    Rows arrive as url_N / key_N / enabled_N. A row whose URL has been
    cleared is dropped, which is how deleting works — no separate button and
    no row indices to keep in sync.
    """
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    from . import airplaneslive as _al

    form = await request.form()
    rows = []
    for i in range(64):
        if ("url_%d" % i) not in form:
            continue
        url = (form.get("url_%d" % i) or "").strip()
        if not url:
            continue                      # cleared = deleted
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        rows.append({"url": url,
                     "api_key": (form.get("key_%d" % i) or "").strip(),
                     "enabled": form.get("enabled_%d" % i) is not None})
    _al.save_endpoints(rows)
    return RedirectResponse(url="/admin/diagnostics", status_code=303)


def build_diagnostics_html(request: Request) -> str:
    """Why is there no ADS-B? Answers it in one block instead of in the logs.

    Was a standalone page at /admin/diagnostics through 1.6.0. It is now a
    SECTION of /admin, so this returns the markup rather than a response.

    Every step of the live path is shown separately, because "no tracking"
    has half a dozen possible causes that look identical from the outside:
    the poller not running, the callsign never resolving, the provider
    returning nothing, or the match logic refusing what it returned. Each
    one is reported on its own line with the actual values involved.
    """
    import html as _html
    import time as _time
    from . import airplaneslive as _al
    from . import poller as _poller
    from .flightmatch import evaluate as _evaluate
    from .livesource import live_state as _live_state

    out = []

    def row(label, value, ok=None):
        colour = "" if ok is None else (
            "color:#137333" if ok else "color:#c5221f;font-weight:600")
        # NO nowrap, and the value is allowed to break mid-token. Values
        # here are raw API output — a 180-character error body, a URL, a
        # refusal reason — and on a phone any one of them used to push the
        # whole page sideways. Breaking an ugly string across two lines is
        # strictly better than a page that scrolls horizontally.
        out.append(
            "<tr><td style='padding:4px 12px 4px 0;color:var(--muted);"
            "vertical-align:top;word-break:break-word'>%s</td>"
            "<td style='padding:4px 0;word-break:break-word;%s'>"
            "<code>%s</code></td></tr>"
            % (_html.escape(str(label)), colour, _html.escape(str(value)))
        )

    out.append("<h2>Poller</h2><table>")
    running = _poller.is_running()
    row("running", running, running)
    last = _poller.last_sweep_at()
    row("last sweep", last.isoformat() if last else "never", last is not None)
    row("sweeps from", "T-%s min" % (_poller.PREVIEW_WINDOW.total_seconds() / 60))
    out.append("</table>")

    # A direct, uncached probe of the provider. This is the single most
    # useful line on the page: if the endpoint has moved or is unreachable,
    # everything downstream is None and nothing else here will make sense.
    # Every configured feed is probed, not just the first, and each row is
    # editable. When a feed shuts its doors — as airplanes.live did — the
    # fix is toggling to another line here rather than a redeploy.
    locked = _al.endpoints_are_locked()
    eps = _al.load_endpoints()
    out.append("<h2>ADS-B feeds</h2>")
    if locked:
        out.append("<p style='color:#c5221f'>Pinned by <code>ADSB_ENDPOINTS</code> "
                   "or <code>AIRPLANES_LIVE_BASE</code> in docker-compose.yml, so "
                   "edits here are ignored. Remove those lines to manage feeds "
                   "from this page.</p>")
    out.append("<form method='post' action='/admin/diagnostics/endpoints'>")
    out.append("<table style='width:100%;border-collapse:collapse'>"
               "<tr style='text-align:left;color:#5f6368;font-size:12px'>"
               "<th style='padding:4px 8px 4px 0'>On</th>"
               "<th style='padding:4px 8px 4px 0'>Feed URL</th>"
               "<th style='padding:4px 8px 4px 0'>API key (blank if none)</th>"
               "<th style='padding:4px 0'>Status</th></tr>")
    working = 0
    for i, e in enumerate(eps):
        status, count, msg = _al.probe(e["url"], api_key=e.get("api_key") or "")
        # 429 means the feed ANSWERED and asked us to slow down. That is
        # proof it is alive, so it counts as working and is coloured amber
        # rather than red. Reporting it as a failure is what made this page
        # show every feed dead while flights were tracking normally.
        ok = (status == 200 and count is not None)
        limited = (status == 429)
        if (ok or limited) and e.get("enabled", True):
            working += 1
        label = ("HTTP %s" % status) if status else "no response"
        detail = "%s — %s" % (label, ("%d aircraft" % count)
                              if count is not None else msg)
        checked = " checked" if e.get("enabled", True) else ""
        out.append(
            "<tr style='border-top:1px solid #e8eaed'>"
            "<td style='padding:8px 8px 8px 0'>"
            "<input type='checkbox' name='enabled_%d'%s></td>"
            "<td style='padding:8px 8px 8px 0'>"
            "<input name='url_%d' value='%s' style='width:100%%;min-width:210px;"
            "padding:5px;font:12px monospace'></td>"
            "<td style='padding:8px 8px 8px 0'>"
            "<input name='key_%d' value='%s' placeholder='none' "
            "style='width:100%%;min-width:110px;padding:5px;font:12px monospace'>"
            "</td>"
            "<td style='padding:8px 0;font-size:12px;%s'>%s</td></tr>"
            % (i, checked, i, _html.escape(e["url"]), i,
               _html.escape(e.get("api_key") or ""),
               "color:#137333" if ok else
               ("color:#b8860b" if limited else "color:#c5221f;font-weight:600"),
               _html.escape(detail))
        )
    # One always-blank row, so adding a feed needs no separate button.
    n = len(eps)
    out.append(
        "<tr style='border-top:1px solid #e8eaed'>"
        "<td style='padding:8px 8px 8px 0'><input type='checkbox' "
        "name='enabled_%d' checked></td>"
        "<td style='padding:8px 8px 8px 0'><input name='url_%d' value='' "
        "placeholder='https://… add a feed' style='width:100%%;min-width:210px;"
        "padding:5px;font:12px monospace'></td>"
        "<td style='padding:8px 8px 8px 0'><input name='key_%d' value='' "
        "placeholder='none' style='width:100%%;min-width:110px;padding:5px;"
        "font:12px monospace'></td>"
        "<td style='padding:8px 0;font-size:12px;color:#5f6368'>new</td></tr>"
        % (n, n, n))
    out.append("</table>")
    out.append("<p style='margin-top:12px'>"
               "<button type='submit' style='padding:8px 16px;font-size:14px'>"
               "Save feeds &amp; re-test</button> "
               "<span style='color:#5f6368;font-size:12px'>"
               "Clear a URL to delete that row. Untick to keep it but skip it."
               "</span></p></form>")
    out.append(
        "<p style='font-size:12px'>Feeds are tried in order until one "
        "answers. Data from <a href='https://adsb.lol'>adsb.lol</a> (ODbL "
        "1.0) and <a href='https://adsb.fi'>adsb.fi</a> (personal, "
        "non-commercial). <a href='https://airplanes.live'>airplanes.live</a> "
        "withdrew its free API in 2026 and is now feeder- or sponsor-only "
        "\u2014 enable it above if you run a receiver or sponsor them, and "
        "put it first.</p>")
    out.append(
        "<form method='post' action='/admin/diagnostics/endpoints/reset' "
        "style='margin-top:8px'><button type='submit' style='padding:6px 12px;"
        "font-size:12px'>Reset feeds to current defaults</button>"
        "<span style='font-size:12px;color:var(--muted);margin-left:8px'>"
        "Use this if the list above still names a feed that shut down "
        "\u2014 a saved list overrides the built-in one.</span></form>")

    out.append("<table>")
    # The probe says whether a feed answers ONE uncached request right now.
    # Recent real lookups say whether tracking is actually working, and
    # that is the question being asked. When they disagree the history
    # wins, so the summary is built from both rather than the probe alone.
    _hist = _al.recent_results()
    _recent_ok = sum(1 for h in _hist[:15] if h.get("ok"))
    if working > 0:
        row("feeds working", "%d enabled and answering" % working, True)
    elif _recent_ok:
        row("feeds working",
            "probe says no, but %d of the last %d REAL lookups succeeded "
            "\u2014 tracking is working and the probe is being rate limited"
            % (_recent_ok, min(len(_hist), 15)), True)
    else:
        row("feeds working", "none answering, and no recent lookup succeeded",
            False)
    out.append("</table>")
    if not working:
        out.append("<p style='color:#c5221f'><b>No enabled feed is answering.</b> "
                   "Every lookup returns nothing and no flight will show live "
                   "tracking, however healthy the rest of the app looks.</p>")

    # The probe above says whether the host answers RIGHT NOW. This says
    # what the poller actually got on real flights, which is the question
    # that matters and can disagree with the probe.
    hist = _al.recent_results()
    out.append("<h2>Recent real lookups</h2>")
    if not hist:
        out.append("<p style='color:#5f6368'>Nothing recorded yet. This fills "
                   "in as the poller looks up flights in the tracking "
                   "window, and survives restarts.</p>")
    else:
        good = sum(1 for h in hist if h.get("ok"))
        out.append("<p style='color:#5f6368;font-size:13px'>%d of the last %d "
                   "lookups succeeded. If these are green while the probe "
                   "above is red, the feed is working and the probe is being "
                   "rate-limited — trust this table.</p>" % (good, len(hist)))
        out.append("<table style='width:100%;border-collapse:collapse;"
                   "font-size:12px'>")
        for h in hist[:15]:
            out.append(
                "<tr style='border-top:1px solid #e8eaed'>"
                "<td style='padding:5px 10px 5px 0;color:#5f6368;"
                "white-space:nowrap'>%s</td>"
                "<td style='padding:5px 10px 5px 0'>%s</td>"
                "<td style='padding:5px 10px 5px 0;font-family:monospace'>%s</td>"
                "<td style='padding:5px 0;%s'>%s</td></tr>"
                % (_html.escape((h.get("at") or "")[5:16].replace("T", " ")),
                   _html.escape(h.get("callsign") or ""),
                   _html.escape((h.get("feed") or "").replace("https://", "")),
                   "color:#137333" if h.get("ok") else "color:#c5221f",
                   _html.escape(h.get("detail") or ""))
            )
        out.append("</table>")

    # WHAT THIS SECTION IS FOR, because it was misleading before 1.8.0.
    # `active_flights()` returns whatever is inside the tracking WINDOW,
    # and a leg stays inside that window until 3 hours past its scheduled
    # arrival whether it has closed or not. So a leg that finished cleanly
    # on the airline's own gate-in an hour ago still appeared here, under a
    # heading saying "active", with a live uncached lookup fired against it
    # — which both looked like the app had failed to let go and spent real
    # ADS-B requests on a finished flight.
    #
    # The poller was always right: `poll_once` skips closed legs. Only this
    # page was wrong. Closed legs are now listed separately and are NOT
    # probed.
    out.append("<h2>Your active flights</h2>")
    active = _poller.active_flights()
    _open, _done = {}, {}
    for _lid, _leg in active.items():
        _r = get_flight(_lid)
        (_done if (_r is not None and _r["closed"]) else _open)[_lid] = (_leg, _r)
    if _done:
        out.append("<p style='color:#5f6368'>Not queried, because they are "
                   "finished — still listed only because they are inside the "
                   "3-hour window: " + ", ".join(
                       "%s (closed by %s)" % (_html.escape(l.flight_number or i),
                                              _html.escape((r["closed_by"] or "?")))
                       for i, (l, r) in _done.items()) + "</p>")
    active = {k: v[0] for k, v in _open.items()}
    if not active:
        out.append("<p>Nothing OPEN in the tracking window right now. The "
                   "poller looks at flights from T-%s minutes until 3 hours "
                   "past scheduled arrival, and stops the moment a leg "
                   "closes.</p>"
                   % (_poller.PREVIEW_WINDOW.total_seconds() / 60))
    for leg_id, leg in active.items():
        out.append("<h3>%s &mdash; %s to %s</h3><table>"
                   % (_html.escape(leg.flight_number or leg_id),
                      _html.escape(leg.origin or "?"),
                      _html.escape(leg.destination or "?")))
        row("callsign searched", leg.callsign or "(none resolved)", bool(leg.callsign))
        if leg.callsign:
            state = None
            try:
                state = _live_state(leg.callsign, cache_ttl_s=0)
            except Exception as e:
                row("lookup", "FAILED: %s" % e, False)
            if state is None:
                row("aircraft found", "NO — nothing broadcasting this callsign", False)
            else:
                row("aircraft found", "yes", True)
                for k in ("icao24", "registration", "lat", "lon",
                          "on_ground", "altitude_ft", "speed_kts",
                          "position_age_s"):
                    row(k, state.get(k))
                verdict = _evaluate(leg, state)
                row("accepted as this leg", verdict.accepted, verdict.accepted)
                if not verdict.accepted:
                    row("reason refused", verdict.reason, False)
        out.append("</table>")

    # Hardcoded light-theme greys were fine on a standalone page; embedded
    # in the themed admin page they were grey-on-black. Swapped for the
    # palette variables so this section follows the theme like everything
    # else. Kept as a string substitution rather than rewriting every row()
    # call, because the generator above is long and entirely mechanical.
    html = "".join(out)
    for dead, live in (("#5f6368", "var(--muted)"),
                       ("#137333", "#22c55e"),
                       ("#c5221f", "#ef4444"),
                       ("#e8eaed", "var(--border)")):
        html = html.replace(dead, live)
    return html


@app.post("/admin/diagnostics/endpoints/reset")
async def admin_reset_endpoints(request: Request):
    """Drop a saved feed list so the built-in defaults apply again."""
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    if not pilot["is_admin"]:
        return RedirectResponse(url="/flights", status_code=303)
    from . import airplaneslive as _al
    _al.reset_endpoints()
    return RedirectResponse(url="/admin#diagnostics", status_code=303)


@app.get("/admin/diagnostics")
async def admin_diagnostics_moved(request: Request):
    """Merged into /admin in 1.7.0. Kept so old links and bookmarks land."""
    return RedirectResponse(url="/admin#diagnostics", status_code=301)



@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, subject: Optional[str] = None,
                     event: Optional[str] = None, q: Optional[str] = None,
                     limit: int = 100, flash: Optional[str] = None):
    """Running the install: people, test mode, diagnostics, decision log.

    ONE PAGE, four stacked sections, deliberately plain. Through 1.6.0
    these were spread across three places — the people table inside
    Settings, diagnostics on its own unstyled page, and the decision log
    reachable only by typing its URL — and /admin itself was the schedule.
    You come here when something is wrong, and hunting for the right page
    is the last thing that should stand in the way.
    """
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    if not pilot["is_admin"]:
        return RedirectResponse(url="/flights", status_code=303)

    settings = load_settings(pilot["id"])
    sim_scenarios = [{"key": sc.key, "title": sc.title,
                      "description": sc.description,
                      "route": f"{sc.origin}–{sc.destination}",
                      "legs": sc.legs, "block_min": sc.block_min}
                     for sc in simulator.SCENARIOS]
    sim_rows = []
    for r in simulator.active_sim_rows():
        sc = simulator.SCENARIOS_BY_KEY.get(r["sim_scenario"] or "")
        sim_rows.append({
            "id": r["id"],
            "callsign": f"{r['flight_number']} {r['origin']}→{r['destination']}",
            "scenario": sc.title if sc else (r["sim_scenario"] or "—"),
            "phase": r["phase_tag"] or "—", "closed": bool(r["closed"]),
            "closed_by": r["closed_by"] or "—",
            "airborne": bool(r["airborne_seen"]), "landed": bool(r["landed_seen"]),
            "stopped_since": r["stopped_since"],
        })

    # 100 by default (1.8.0, was 200). A phone rendering 200 monospace
    # lines of JSON is slow to open and slower to scroll, and the reason to
    # open this page is almost always the last handful of decisions.
    limit = max(10, min(int(limit), 2000))
    log_events, log_names, log_max_id = [], [], 0
    if debuglog.ENABLED:
        log_events = debuglog.recent(limit=limit, subject=subject,
                                     event=event, q=q)
        log_names = debuglog.event_names()
        log_max_id = max((e["id"] for e in log_events), default=0)

    template = jinja_env.get_template("admin.html")
    return HTMLResponse(template.render(
        request=request, settings=settings.model_dump(),
        people=list_all_users(), pilot_id=pilot["id"],
        sim_rows=sim_rows, sim_scenarios=sim_scenarios,
        diagnostics_html=Markup(build_diagnostics_html(request)),
        log_enabled=debuglog.ENABLED, log_events=log_events,
        log_subject=subject or "", log_event=event or "", log_q=q or "",
        log_limit=limit, log_names=log_names, log_max_id=log_max_id,
        log_limits=[50, 100, 250, 500, 1000],
        flash=FLASHES.get(flash or ""), flash_kind=FLASH_KIND.get(flash or "", "err"),
        active_tab=None, is_admin=True, is_pilot=True,
    ))


@app.get("/admin/log/tail")
async def admin_log_tail(request: Request, after_id: int = 0,
                         subject: Optional[str] = None,
                         event: Optional[str] = None,
                         q: Optional[str] = None):
    """JSON feed of log lines NEWER than after_id. Drives the live tail.

    Polled rather than streamed. A WebSocket or SSE would be tidier, but
    this app is deployed behind whatever reverse proxy the owner happens to
    be running on a NAS, and a long-lived connection is the first thing
    such a proxy drops. A 2-second poll for "anything newer than id N"
    returns an empty list almost every time and costs nothing.
    """
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return JSONResponse({"events": [], "max_id": after_id}, status_code=403)
    if not pilot["is_admin"] or not debuglog.ENABLED:
        return JSONResponse({"events": [], "max_id": after_id})
    rows = debuglog.recent(limit=200, subject=subject, event=event, q=q,
                           after_id=after_id or None)
    rows.reverse()                      # oldest first, so the tail appends
    return JSONResponse({
        "events": [{"id": r["id"], "at": r["at"], "event": r["event"],
                    "subject": r["subject"],
                    "detail": json.dumps(r["detail"], default=str)}
                   for r in rows],
        "max_id": max([r["id"] for r in rows] + [after_id or 0]),
    })


@app.get("/admin/log/download")
async def admin_log_download(request: Request, limit: int = 500,
                             subject: Optional[str] = None,
                             event: Optional[str] = None,
                             q: Optional[str] = None):
    """The current view, as a text file, for pasting into a conversation.

    Plain text rather than JSON: the point is to be readable by a person
    who has been handed it, and the same filters that are on screen apply,
    so what downloads is what you were looking at.
    """
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    if not pilot["is_admin"]:
        return RedirectResponse(url="/flights", status_code=303)
    rows = debuglog.recent(limit=max(10, min(int(limit), 5000)),
                           subject=subject, event=event, q=q)
    rows.reverse()
    lines = [f"MyPilot decision log — {len(rows)} events",
             f"exported {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
             f"filters: subject={subject or '-'} event={event or '-'} search={q or '-'}",
             "-" * 72]
    for r in rows:
        lines.append(f"{r['at']}  {r['event']}  {r['subject'] or ''}")
        for k, v in (r["detail"] or {}).items():
            lines.append(f"    {k}: {v}")
    body = "\n".join(lines) + "\n"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return PlainTextResponse(body, headers={
        "Content-Disposition": f'attachment; filename="mypilot-log-{stamp}.txt"'})


@app.get("/admin/debug")
async def admin_debug_moved(request: Request):
    """Merged into /admin in 1.7.0."""
    return RedirectResponse(url="/admin#log", status_code=301)


@app.post("/admin/debug/clear")
async def admin_debug_clear(request: Request):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    if not pilot["is_admin"]:
        return RedirectResponse("/", status_code=303)
    debuglog.clear()
    return RedirectResponse("/admin#log", status_code=303)


@app.get("/flights", response_class=HTMLResponse)
async def flights_page(request: Request, month: Optional[str] = None,
                       err: Optional[str] = None):
    """One pilot's SCHEDULE. Renamed from /admin in 1.7.0.

    The tab bar had called this page "Flights" since v7.5 while its URL
    said /admin, and 1.6.0 then stacked the install's administration on
    top of it. Two scopes on one page under a name that matched neither.
    Administration is now at /admin, which is what that word means.
    """
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot

    settings = load_settings(pilot["id"])
    info = get_current_info(pilot["id"])
    upcoming_legs = ([info.current] if info.current else []) + list(info.upcoming)
    past_legs = info.past

    # Month filter (1.5.0). Now that imports ACCUMULATE rather than roll
    # over (N1), this table grows by ~46 legs a month and would otherwise
    # be an unbroken scroll by the end of a year. Filtering server-side
    # rather than hiding rows in the browser, because the point is to stop
    # sending 550 rows to a phone, not to stop showing them.
    #
    # Months are offered NEWEST FIRST: the reason to open this page is
    # almost always the trip you are on or the one just flown.
    all_legs = list(upcoming_legs) + list(past_legs)
    months = sorted({l.date.strftime("%Y-%m") for l in all_legs}, reverse=True)
    month_options = [{
        "value": m,
        "label": datetime.strptime(m, "%Y-%m").strftime("%B %Y"),
        "count": sum(1 for l in all_legs if l.date.strftime("%Y-%m") == m),
    } for m in months]
    # An unknown or stale month (a bookmark from a month since purged)
    # falls back to showing everything rather than an empty page.
    active_month = month if month in months else None
    if active_month:
        upcoming_legs = [l for l in upcoming_legs
                         if l.date.strftime("%Y-%m") == active_month]
        past_legs = [l for l in past_legs
                     if l.date.strftime("%Y-%m") == active_month]

    def build_rows(legs):
        rows = []
        for i, leg in enumerate(legs):
            if leg.trip_start and i > 0:
                rows.append({"divider": True})
            rows.append({
                "id": leg.id,
                # Was str(leg.date) — a raw ISO "2026-08-15" in the one
                # table a person reads to check their own schedule, while
                # the import review two clicks earlier said "August 15".
                # Same app, same data, two formats.
                "date": leg.date.strftime("%b %d").replace(" 0", " "),
                "date_iso": str(leg.date),
                "callsign": leg.callsign,
                "route": f"{leg.origin} → {leg.destination}",
                "dep": fmt_local(leg, "dep", settings.time_format, with_zone=False),
                "arr": fmt_local(leg, "arr", settings.time_format, with_zone=False),
                "dep_zone": tz_abbr(leg, "dep"),
                "arr_zone": tz_abbr(leg, "arr"),
                "same_zone": tz_abbr(leg, "dep") == tz_abbr(leg, "arr"),
                "is_deadhead": leg.is_deadhead,
            })
        return rows

    upcoming_rows = build_rows(upcoming_legs)
    past_rows = build_rows(past_legs)

    template = jinja_env.get_template("flights.html")
    return HTMLResponse(template.render(
        request=request, upcoming_rows=upcoming_rows, past_rows=past_rows,
        past_count=len(past_legs),
        count=len(upcoming_legs) + len(past_legs),
        total_count=len(all_legs),
        month_options=month_options, active_month=active_month,
        active_month_label=(datetime.strptime(active_month, "%Y-%m").strftime("%B %Y")
                            if active_month else None),
        active_tab="flights", is_admin=bool(pilot["is_admin"]), is_pilot=True,
        settings=settings.model_dump(), share_code=pilot["share_code"],
        # A paste that parsed to nothing has always redirected here with
        # ?err=parse, and this page has always ignored it — so the paste
        # box emptied itself, the roster did not change, and NOTHING said
        # why. Silence is the worst answer to "did that work", because
        # the pilot's next move is to paste it again.
        import_error=(err == "parse"),
    ))


@app.get("/admin/legacy-schedule")
async def admin_legacy_schedule(request: Request):
    """/admin used to be the schedule. Anything bookmarked lands here."""
    return RedirectResponse(url="/flights", status_code=301)


@app.post("/flights/import")
async def admin_import(request: Request, text: str = Form(...)):
    """Show what this paste would change. Applies NOTHING. (N1, 1.5.0)

    Through 1.4.0 this page listed the paste and the confirm step replaced
    the roster with it, so a leg's absence from the paste was silently a
    deletion. Now the page is a DIFF, and the two failures that never
    announce themselves — a dropped trip the pilot forgot to remove, and a
    leg flown that was never on the line — are both visible in one place.
    """
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    legs = parse_schedule_text(text)
    if not legs:
        # Nothing valid parsed — nothing to review, just go back.
        return RedirectResponse(url="/flights?err=parse", status_code=303)
    apply_gap_trip_starts(legs)
    settings = load_settings(pilot["id"])

    now = datetime.now(timezone.utc)
    diff = build_diff(legs, load_schedule(pilot["id"]), now)
    review_legs = build_review_legs(legs, settings.time_format)
    template = jinja_env.get_template("import_review.html")
    return HTMLResponse(template.render(
        request=request, legs=review_legs, settings=settings.model_dump(),
        scope_label=month_labels(months_covered(legs)),
        added=build_diff_rows(diff[ADDED], settings.time_format),
        changed=build_diff_rows(diff[CHANGED], settings.time_format),
        # SPLIT, because they are two different statements. An upcoming
        # leg missing from the paste is the paste CONTRADICTING it, and is
        # ticked. A flown leg missing from the paste is the paste being
        # SILENT about it — routine, since one trip can be pasted on its
        # own — so it is offered unticked. See app/importer.py.
        removed=[r for r in build_diff_rows(diff[REMOVED], settings.time_format)
                 if not r["flown"]],
        removed_flown=[r for r in build_diff_rows(diff[REMOVED], settings.time_format)
                       if r["flown"]],
        unchanged_count=len(diff[UNCHANGED]),
    ))


@app.post("/flights/import/confirm")
async def admin_import_confirm(request: Request):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    form = await request.form()
    dates = form.getlist("leg_date")
    flights = form.getlist("leg_flight")
    origins = form.getlist("leg_origin")
    dests = form.getlist("leg_dest")
    deps = form.getlist("leg_dep")
    arrs = form.getlist("leg_arr")
    dhs = form.getlist("leg_dh")
    trip_starts = form.getlist("leg_trip_start")

    legs = []
    for i in range(len(dates)):
        is_dh = dhs[i] == "1"
        # First leg is always a trip start regardless of what the client
        # computed — a safety net, not just a default, in case JS ever
        # fails to run for some reason.
        trip_start = (i == 0) or (i < len(trip_starts) and trip_starts[i] == "1")
        leg_id = f"{dates[i]}-{flights[i]}-{origins[i]}-{dests[i]}"
        if is_dh:
            leg_id += "-DH"
        leg = FlightLeg(
            id=leg_id,
            date=date.fromisoformat(dates[i]),
            flight_number=flights[i],
            origin=origins[i],
            destination=dests[i],
            dep_time_local=dtime.fromisoformat(deps[i]),
            arr_time_local=dtime.fromisoformat(arrs[i]),
            is_deadhead=is_dh,
            trip_start=trip_start,
        )
        enrich_leg(leg)
        legs.append(leg)

    # ADD, never replace. The only legs that go are the ones the pilot
    # left ticked on the review page, and the review page only ever offers
    # future legs inside the months this paste covers — so importing
    # September cannot touch August, and no import can revise history.
    merge_schedule(pilot["id"], legs)
    removals = [r for r in form.getlist("remove_id") if r]
    if removals:
        offered = set(form.getlist("removable_id"))
        # Only honour removals the page actually offered. Without this a
        # crafted form could delete any leg on the roster, including flown
        # ones, which is the exact class of silent history edit N1 exists
        # to prevent.
        remove_legs(pilot["id"], [r for r in removals if r in offered])
    return RedirectResponse(url="/flights", status_code=303)


@app.post("/admin/import/confirm")
async def admin_import_confirm_legacy(request: Request):
    """Moved to /flights/import/confirm in 1.7.0. Kept as a redirect.

    THIS WAS NOT KEPT AT THE TIME, and that is the whole bug. 1.7.0 split
    /admin into /flights and /admin and moved every route with it, but
    import_review.html's form action was left pointing at the old path.
    The page rendered, the diff was correct, every leg was listed — and
    Confirm & Import posted into a 404. What the pilot saw was FastAPI's
    bare `{"detail":"Not Found"}`, which is not recognisable as a missing
    route unless you already know what it is.

    It survived nine releases because nothing exercised the button: the
    tests rendered the review page and asserted on its markup, and the
    confirm route was tested by calling it directly. Each half passed
    while the join between them was broken. There is now a test that
    posts to the action the TEMPLATE names rather than to the path the
    test author remembers.

    307, not 303: this is a POST carrying the entire parsed schedule, and
    303 would turn it into a GET and silently drop every leg. That is a
    worse failure than the 404, because it looks like it worked.
    """
    return RedirectResponse(url="/flights/import/confirm", status_code=307)


@app.post("/flights/delete/{leg_id}")
async def admin_delete(request: Request, leg_id: str):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    delete_leg(pilot["id"], leg_id)
    return RedirectResponse(url="/flights", status_code=303)


# ---------------------------------------------------------------------------
# Admin-only: test mode, and the people panel
# ---------------------------------------------------------------------------

@app.post("/admin/test/start")
async def admin_test_start(request: Request, scenario: str = Form(...)):
    """Begin a rehearsal. Costs nothing and touches no real flight."""
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    if not pilot["is_admin"]:
        return RedirectResponse(url="/flights", status_code=303)
    simulator.start(pilot["id"], scenario)
    return RedirectResponse(url="/admin#test-mode", status_code=303)


@app.post("/admin/test/stop")
async def admin_test_stop(request: Request):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    if not pilot["is_admin"]:
        return RedirectResponse(url="/flights", status_code=303)
    simulator.stop(pilot["id"])
    return RedirectResponse(url="/admin#test-mode", status_code=303)


@app.post("/admin/test/age")
async def admin_test_age(request: Request, leg_id: str = Form(...),
                         minutes: int = Form(30)):
    """Shift a simulated leg's observed timestamps backwards.

    The alternative to a clock multiplier. Nothing is faked and no
    threshold is lowered — see simulator.age.
    """
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    if not pilot["is_admin"]:
        return RedirectResponse(url="/flights", status_code=303)
    # Bounded, and only ever backwards. Ageing FORWARD would put stored
    # events in the future, which nothing downstream is written to expect.
    simulator.age(leg_id, max(1, min(int(minutes), 720)))
    return RedirectResponse(url="/admin#test-mode", status_code=303)


@app.post("/admin/users/promote/{user_id}")
async def admin_promote_user(request: Request, user_id: int,
                             password: str = Form(...)):
    """Make another account an admin. (1.6.0)

    Until now the ONLY admin was whoever registered first — `create_user`
    sets `is_admin` on the first account and there was no route, button or
    documented method to make a second one. On a box the owner cannot SSH
    into, that means losing the first account loses admin entirely, with
    the data still sitting there.

    Promotion is deliberately not symmetric with demotion below: see there.
    """
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    if not pilot["is_admin"]:
        return RedirectResponse(url="/flights", status_code=303)
    # THE PASSWORD GATE (1.7.0). A single tap was too little friction for
    # an irreversible grant: an admin can then see and delete every account
    # on the install, including the one that promoted them. Re-entering the
    # password also means an unlocked phone left on a crew room table is
    # not enough on its own.
    if not verify_password(password, pilot["password_hash"]):
        return RedirectResponse(url="/admin?flash=badpw#people", status_code=303)
    set_admin(user_id, True)
    return RedirectResponse(url="/admin?flash=promoted#people", status_code=303)


@app.post("/admin/users/demote/{user_id}")
async def admin_demote_user(request: Request, user_id: int,
                            password: str = Form(...)):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    if not pilot["is_admin"]:
        return RedirectResponse(url="/flights", status_code=303)
    if not verify_password(password, pilot["password_hash"]):
        return RedirectResponse(url="/admin?flash=badpw#people", status_code=303)
    if user_id == pilot["id"]:
        # An admin may not demote themselves. Combined with the last-admin
        # guard in set_admin, this makes it impossible to end up with a
        # database that has data in it and nobody able to administer it —
        # a state with no recovery path short of editing SQLite by hand.
        return RedirectResponse(url="/admin#people", status_code=303)
    ok = set_admin(user_id, False)
    return RedirectResponse(
        url=f"/admin?flash={'demoted' if ok else 'lastadmin'}#people",
        status_code=303)


@app.post("/admin/users/delete/{user_id}")
async def admin_delete_user(request: Request, user_id: int):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    if not pilot["is_admin"]:
        return RedirectResponse(url="/flights", status_code=303)
    if user_id == pilot["id"]:
        return RedirectResponse(url="/admin#people", status_code=303)
    delete_user(user_id)
    return RedirectResponse(url="/admin#people", status_code=303)


@app.post("/flights/regenerate-code")
async def admin_regenerate_code(request: Request):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    regenerate_share_code(pilot["id"])
    return RedirectResponse(url="/flights", status_code=303)


# ---------------------------------------------------------------------------
# Settings — pilot only
# ---------------------------------------------------------------------------

def poller_status(time_format: str = "24") -> dict:
    """What the background tracker is doing, for the Settings page.

    Two separate facts that were previously conflated into one "22 hours
    ago" figure: when the TRACKER last swept, and when the AeroAPI SPEND
    READING was last pulled. A stale spend reading with a healthy tracker
    means the usage endpoint is unhappy; both stale means the tracker
    itself has stopped. One number could not tell those apart.
    """
    from . import poller
    when = poller.last_sweep_at()
    if when is None:
        return {"running": poller.is_running(), "when": None,
                "label": "not since restart", "stale": True}
    age = (datetime.now(ZoneInfo("UTC")) - when).total_seconds()
    local = when.astimezone(ZoneInfo("America/Chicago"))
    fmt = "%a %H:%M" if time_format == "24" else "%a %I:%M %p"
    return {
        "running": poller.is_running(),
        "when": when.isoformat(),
        "label": local.strftime(fmt).replace(" 0", " "),
        # Sweeps run every 20 seconds, so anything past two minutes means
        # the thread is wedged, not merely between ticks.
        "stale": age > 120,
    }


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    s = load_settings(pilot["id"])
    template = jinja_env.get_template("settings.html")
    ctx = {"request": request, "s": s, "saved": False, "is_admin": bool(pilot["is_admin"]),
           "is_pilot": True, "post_to": "/settings", "icon_styles": ICON_STYLES,
           "pilot_id": pilot["id"], "aeroapi_stats": budget_state(pilot["id"]),
           "poller": poller_status(s.time_format), "active_tab": "settings"}
    # NO all_users here any more (1.6.0). Administering the install moved
    # to /admin in one piece; leaving a second copy of the people table
    # behind would mean two places to keep in step and two places to check
    # when something looks wrong.
    return HTMLResponse(template.render(**ctx))


def _clean_budget(raw, fallback: float) -> float:
    """Parse the monthly spend limit from the settings form.

    Anything unparseable keeps the stored value — silently resetting a
    pilot's cap to a default because they fat-fingered a character is the
    one failure this field can't afford.
    """
    try:
        value = float(str(raw).strip().lstrip("$").replace(",", ""))
    except (TypeError, ValueError):
        return round(float(fallback), 2)
    return round(max(0.0, min(500.0, value)), 2)


@app.post("/settings")
async def settings_save(
    request: Request,
    aeroapi_enabled: str = Form(""),
    aeroapi_key: str = Form(""),
    # Default is EMPTY, not "4.90": FastAPI substitutes the declared default
    # for a blank field, so a numeric default here would silently reset a
    # pilot's cap whenever the input was cleared. Blank means "keep stored".
    aeroapi_budget: str = Form(""),
    time_format: str = Form("24"),
    theme: str = Form("dark"),
    poll_seconds: int = Form(15),
    show_flightaware: Optional[str] = Form(None),
    show_fr24: Optional[str] = Form(None),
    icon_style: str = Form("modern"),
):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    s = AppSettings(
        aeroapi_enabled=bool(aeroapi_enabled),
        # A blank key with the toggle on means "keep what's stored" — the
        # form shows the key masked, so submitting shouldn't wipe it.
        aeroapi_key=aeroapi_key.strip() or load_settings(pilot["id"]).aeroapi_key,
        # Clamped, not trusted: this comes from a free-text number input,
        # and a negative or absurd value would either disable tracking
        # silently or defeat the point of having a cap at all. 0 is a valid
        # choice and means stop querying entirely. Taken as a string and
        # parsed here rather than declared float so that a typo returns the
        # settings page with the old value intact, instead of FastAPI's raw
        # 422 error page.
        aeroapi_budget=_clean_budget(aeroapi_budget, load_settings(pilot["id"]).aeroapi_budget),
        time_format="12" if time_format == "12" else "24",
        theme="light" if theme == "light" else "dark",
        poll_seconds=max(10, min(300, int(poll_seconds))),
        show_flightaware=show_flightaware is not None,
        show_fr24=show_fr24 is not None,
        # Validated against the known set rather than stored as submitted:
        # this value is interpolated into the manifest and used to pick an
        # icon filename, so an unknown string would 404 every icon and leave
        # the installed app with a blank tile.
        icon_style=icon_style if icon_style in ICON_STYLES else "modern",
    )
    save_settings(pilot["id"], s)
    template = jinja_env.get_template("settings.html")
    ctx = {"request": request, "s": s, "saved": True, "is_admin": bool(pilot["is_admin"]),
           "is_pilot": True, "post_to": "/settings", "icon_styles": ICON_STYLES,
           "pilot_id": pilot["id"], "aeroapi_stats": budget_state(pilot["id"]),
           "poller": poller_status(s.time_format), "active_tab": "settings"}
    # NO all_users here any more (1.6.0). Administering the install moved
    # to /admin in one piece; leaving a second copy of the people table
    # behind would mean two places to keep in step and two places to check
    # when something looks wrong.
    return HTMLResponse(template.render(**ctx))


@app.post("/settings/users/delete/{user_id}")
async def settings_delete_user(request: Request, user_id: int):
    """Moved to /admin/users/delete in 1.6.0. Kept as a redirect.

    A phone with Settings still open from before the update would post
    here, and a 404 on a DELETE button is the worst possible way to find
    out a route moved — the admin cannot tell whether it happened.
    """
    return RedirectResponse(url="/admin#people", status_code=307)


# ---------------------------------------------------------------------------
# App shell: icon styles, manifest, service worker
# ---------------------------------------------------------------------------

# The plane silhouettes a user can choose between. Keys are the filename stems
# written by make_icons.py — keep the two in step or an icon 404s to a blank
# tile. Order is the order shown in settings.
ICON_STYLES = {
    "modern":  "Modern",
    "sharp":   "Sharp",
    "rounded": "Rounded",
    "delta":   "Delta",
}
DEFAULT_ICON_STYLE = "modern"


def _icon_style_for(request: Request) -> str:
    """Whichever style this visitor should get, falling back safely.

    A viewer (family member) has no settings row of their own, so they
    inherit the pilot's choice — the point is that the household sees one
    consistent app, not that each phone picks its own.
    """
    try:
        pilot = current_pilot(request)
        uid = pilot["id"] if pilot else current_viewer_user_id(request)
        if uid:
            style = load_settings(uid).icon_style
            if style in ICON_STYLES:
                return style
    except Exception:
        # The manifest is requested before login on a cold install, and a
        # broken manifest blocks installation entirely. Never raise here.
        pass
    return DEFAULT_ICON_STYLE


@app.get("/manifest.webmanifest")
async def web_manifest(request: Request):
    """The install descriptor, generated per user so the icon choice applies.

    IMPORTANT AND COUNTERINTUITIVE: changing this does NOT change an icon
    that is already sitting on someone's home screen. Both iOS and Android
    read the manifest once, at install time, and then keep what they got.
    Changing the setting updates the map marker instantly and the installed
    icon only after the app is removed and re-added. Say so in the UI rather
    than letting people think it is broken — the settings page does.

    A native shell can do this properly (iOS has an alternate-icon API), so
    this limitation goes away at the point we ship one, not before.
    """
    style = _icon_style_for(request)
    return JSONResponse({
        "name": "MyPilot",
        "short_name": "MyPilot",
        "description": "Live flight tracking and schedule for airline crew and their families",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0f1419",
        # Matches --bg. It used to be #1e3a8a, which produced a blue flash
        # on launch before the app's own dark background painted.
        "theme_color": "#0f1419",
        "icons": [
            {"src": f"/static/icon-{style}-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": f"/static/icon-{style}-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
            {"src": f"/static/icon-{style}-maskable-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    }, media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    """Serve the worker from the ROOT, with the version baked in.

    Two things are happening here and both matter:

    1. SCOPE. A service worker may only control URLs at or below the path it
       was served from. Served from /static/sw.js it could never see a page
       navigation or an /api/ call, which is everything we need it for.
       Serving it at /sw.js gives it the whole origin.

    2. CACHE KEYING. __APP_VERSION__ is replaced with the running build's
       version, so every deploy produces a different cache name and the
       worker's activate step discards the old one. Without this a phone
       would keep serving the previous build's CSS and JS indefinitely and
       `update.sh` would appear to do nothing.

    no-store on the worker itself is deliberate: browsers cache sw.js, and a
    cached worker cannot ship the fix that replaces itself.
    """
    src = (BASE / "static" / "sw.js").read_text()
    src = src.replace("__APP_VERSION__", VERSION)
    return Response(content=src, media_type="application/javascript",
                    headers={"Cache-Control": "no-store, max-age=0"})


# ---------------------------------------------------------------------------
# JSON API — pilot or viewer, same rules as the tracker page
# ---------------------------------------------------------------------------

# Every endpoint below is mounted TWICE: once under /api/v1/ and once at the
# bare /api/ path it has always had.
#
# The versioned path is the one any future client should call. The bare path
# is an alias kept alive so the currently deployed pages (and anything a
# family member has open right now) do not break the moment this ships.
#
# The distinction matters because of clients we cannot reach. A browser
# always has the newest build seconds after a deploy; an installed app does
# not, and may be months behind. When a field is renamed or removed, v1 keeps
# serving the old shape and the new shape goes to /api/v2/ — so an old build
# keeps working instead of going blank. Adding a field is not a break and
# does not need a new version. See app/version.py.

@app.get("/api/v1/meta")
async def api_meta(client: Optional[str] = None):
    """What this server is, and whether the caller is new enough to talk to it.

    Nothing needs this today: every client is a browser holding code this
    server handed it seconds ago, so client and server cannot disagree. It
    exists because that stops being true the day a native app ships, and
    retrofitting it then means the builds already in the wild are exactly the
    ones that cannot use it.

    A client passes its own build as ?client=1.2.3 on launch. `supported`
    false means the contract has moved past what it understands and it should
    show an update prompt rather than render a blank screen. Omitting the
    parameter is fine and reports supported, so a caller that does not care
    is not forced to.

    Deliberately unauthenticated: a client has to be able to discover it is
    too old to log in.
    """
    supported = client_is_supported(client) if client else True
    return {
        "app": "MyPilot",
        "version": VERSION,
        "api_version": API_VERSION,
        "min_client_version": MIN_CLIENT_VERSION,
        "client_version": client,
        "supported": supported,
    }


@app.get("/api/v1/current")
@app.get("/api/current")
async def api_current(request: Request):
    pilot = current_pilot(request)
    viewer_uid = None if pilot else current_viewer_user_id(request)
    user_id = pilot["id"] if pilot else viewer_uid
    if not user_id:
        return {"error": "not authenticated"}
    info = get_current_info(user_id)
    return info.model_dump(mode="json")


@app.get("/api/v1/selected")
@app.get("/api/selected")
async def api_selected(request: Request, leg: Optional[str] = None):
    """Lightweight polling endpoint: just the live/progress data for the
    selected flight, as JSON. Used by the tracker page to refresh the map
    and stats in place every poll cycle, instead of reloading the whole
    page (which used to reset scroll position, collapse state, and any
    manual map pan/zoom every single refresh)."""
    pilot = current_pilot(request)
    viewer_uid = None if pilot else current_viewer_user_id(request)
    user_id = pilot["id"] if pilot else viewer_uid
    if not user_id:
        return {"error": "not authenticated"}

    settings = load_settings(user_id)
    info = get_current_info(user_id)
    now = datetime.now(ZoneInfo("UTC"))
    tf = settings.time_format
    if not pilot:
        cookie_tf = request.cookies.get("pt_viewer_tf")
        if cookie_tf in ("12", "24"):
            tf = cookie_tf
    selected_leg, is_selected_live = resolve_selected_leg(info, leg, now)
    if not selected_leg:
        return {"error": "no flight"}
    live, extra = compute_live_payload(user_id, selected_leg, is_selected_live, now, settings.poll_seconds, tf)
    return {
        "is_selected_live": is_selected_live,
        # Which leg the app currently considers active. The page compares
        # this to what it's showing so it can switch flights on its own
        # when one ends and the next begins — that used to require the
        # five-minute full-page reload.
        "current_leg_id": info.current.id if info.current else None,
        "live": live,
        "status": extra.get("status"),
        "status_tag": extra.get("status_tag"),
        "phase_tag": extra.get("phase_tag"),
        "signal_note": extra.get("signal_note"),
        "waiting_on_airline": extra.get("waiting_on_airline"),
        "progress_pct": extra.get("progress_pct"),
        "ete": extra.get("ete"),
        "dep_delay": extra.get("dep_delay"),
        "arr_delay": extra.get("arr_delay"),
        "dep_line": extra.get("dep_line"),
        "arr_line": extra.get("arr_line"),
        "enriched_at": extra.get("enriched_at"),
        "enriched_at_iso": extra.get("enriched_at_iso"),
        "last_signal_iso": extra.get("last_signal_iso"),
        "closed": extra.get("closed"),
        "closed_by": extra.get("closed_by"),
        "arrival_source": extra.get("arrival_source"),
        "dep_shown": extra.get("dep_shown"),
        "arr_shown": extra.get("arr_shown"),
        "gates": extra.get("gates"),
        "diversion": extra.get("diversion"),
        "distance_nm": extra.get("distance_nm"),
        "breadcrumb": extra.get("breadcrumb", []),
        "aircraft": extra.get("aircraft"),
        "origin": {"lat": selected_leg.origin_info.lat, "lon": selected_leg.origin_info.lon} if selected_leg.origin_info else None,
        "destination": {"lat": selected_leg.dest_info.lat, "lon": selected_leg.dest_info.lon} if selected_leg.dest_info else None,
    }


@app.get("/api/v1/leg/{leg_id}")
@app.get("/api/leg/{leg_id}")
async def api_leg(request: Request, leg_id: str):
    """Everything the tracker page needs to switch to a different flight
    without navigating.

    Tapping a flight row used to be a full page load, which reset scroll
    position, re-collapsed the card, and closed the past-flights list. This
    returns the same data the server-rendered page would have, so the page
    can swap it in place.
    """
    pilot = current_pilot(request)
    viewer_uid = None if pilot else current_viewer_user_id(request)
    user_id = pilot["id"] if pilot else viewer_uid
    if not user_id:
        return {"error": "not authenticated"}

    settings = load_settings(user_id)
    info = get_current_info(user_id)
    now = datetime.now(ZoneInfo("UTC"))

    tf = settings.time_format
    if not pilot:
        cookie_tf = request.cookies.get("pt_viewer_tf")
        if cookie_tf in ("12", "24"):
            tf = cookie_tf

    selected_leg, is_selected_live = resolve_selected_leg(info, leg_id, now)
    if not selected_leg:
        return {"error": "no flight"}

    view = leg_view(selected_leg, now, tf, tag_index(user_id))
    live, extra = compute_live_payload(user_id, selected_leg, is_selected_live, now, settings.poll_seconds, tf)
    if view:
        view.update(extra)
    return {
        "leg_id": selected_leg.id,
        "is_selected_live": is_selected_live,
        "current_leg_id": info.current.id if info.current else None,
        "live": live,
        # THE PATH IT ACTUALLY FLEW (1.20.0). Read straight from the
        # positions table rather than out of the live payload, because
        # that payload is about a flight in progress and hands back an
        # empty breadcrumb for a leg that finished months ago — which is
        # exactly the leg the calendar is asking about.
        #
        # Recording tracks for a year is only worth the rows if something
        # reads them back. This is that something.
        "breadcrumb": get_breadcrumb(selected_leg.id),
        "current": view,
    }
