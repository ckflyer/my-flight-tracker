from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pathlib import Path
from datetime import datetime, date, timedelta, time as dtime
from zoneinfo import ZoneInfo
from typing import Optional
import calendar as cal_module
from types import SimpleNamespace
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .schedule import load_schedule, get_current_info, delete_leg, save_schedule
from .enrichment import query_stats, budget_state
from .flights import get_flight
from .models import FlightLeg
from .parser import parse_schedule_text
from .airports import enrich_leg
from .settings import load_settings, save_settings, AppSettings
from .track import get_breadcrumb
from . import tags
from . import view as flight_view
from .auth import (
    get_or_create_secret_key, count_users, create_user, get_user_by_username,
    get_user_by_id, get_user_by_share_code, verify_password, regenerate_share_code,
    list_all_users, delete_user, set_recovery_code, reset_password_with_recovery_code,
)
from .ratelimit import check_rate_limit
from .version import VERSION

BASE = Path(__file__).resolve().parent.parent
jinja_env = Environment(
    loader=FileSystemLoader(str(BASE / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
)
jinja_env.globals["version"] = VERSION

app = FastAPI(title="Pilot Tracker")
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
    tz = ZoneInfo(info.timezone)
    sample = datetime(2026, 7, 1, 12, 0, tzinfo=tz)
    abbr = sample.tzname() or info.timezone.split("/")[-1]
    return f"{time_str} {abbr}"


# Daylight and standard forms of the same zone collapse to one label:
# "CDT" and "CST" both become "CT". Two characters instead of three, and
# it is what consumer apps show.
#
# This RETIRES a bug rather than fixing it. tz_abbr and fmt_local both
# sample a fixed July date to get an abbreviation, so every leg read as
# daylight time and a December flight said CDT where CST was correct. A
# label that never claims daylight or standard cannot be wrong about which
# one applies, so the July sample stops mattering.
#
# Phoenix is the loose end: Arizona skips daylight time, so MT is a broad
# name for it. Still the right zone, and no worse than the MST it showed.
_TWO_LETTER_ZONE = {
    "EST": "ET", "EDT": "ET",
    "CST": "CT", "CDT": "CT",
    "MST": "MT", "MDT": "MT",
    "PST": "PT", "PDT": "PT",
    "AKST": "AKT", "AKDT": "AKT",
    "HST": "HT", "HDT": "HT",
    "AST": "AT", "ADT": "AT",
}


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
    tz = ZoneInfo(info.timezone)
    sample = datetime(2026, 7, 1, 12, 0, tzinfo=tz)
    raw = sample.tzname() or info.timezone.split("/")[-1]
    # Anything outside North America keeps whatever the zone database calls
    # it rather than being forced into a shape it does not have.
    return _TWO_LETTER_ZONE.get(raw, raw)


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
    return f"{text} {local.tzname() or ''}".strip()


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


def leg_view(leg: Optional[FlightLeg], now: datetime, time_format: str = "24",
             tag_lookup: Optional[dict] = None) -> Optional[dict]:
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
                      overnights: Optional[dict] = None) -> list:
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
            "legs": [leg_view(l, now, time_format, tags_by_leg) for l in bucket["legs"]],
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

def resolve_selected_leg(info, leg_id: Optional[str]):
    """Which flight is the map/collapsed card showing? Default: the
    genuinely active flight if there is one, else the next upcoming one,
    else the most recent past one. A leg_id (from tapping a flight in the
    list) overrides that, as long as it's a real leg on this schedule."""
    selected_leg = info.current or (info.upcoming[0] if info.upcoming else None) or (info.past[-1] if info.past else None)
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


def build_flight_list(info, day_numbers: dict, now: datetime, time_format: str,
                      tags_by_leg, overnights: dict) -> list:
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

    groups = group_legs_by_day(ordered, day_numbers, now, time_format,
                               tags_by_leg, overnights)
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
    tf = settings.time_format
    display_theme = settings.theme
    show_fa = settings.show_flightaware
    show_fr24 = settings.show_fr24
    if not pilot:
        # Viewers can override display prefs for themselves via cookies set
        # from /viewer-settings — never touches the pilot's actual account.
        cookie_tf = request.cookies.get("pt_viewer_tf")
        if cookie_tf in ("12", "24"):
            tf = cookie_tf
        cookie_theme = request.cookies.get("pt_viewer_theme")
        if cookie_theme in ("dark", "light"):
            display_theme = cookie_theme
        if "pt_viewer_show_fa" in request.cookies:
            show_fa = request.cookies.get("pt_viewer_show_fa") == "1"
        if "pt_viewer_show_fr24" in request.cookies:
            show_fr24 = request.cookies.get("pt_viewer_show_fr24") == "1"

    selected_leg, is_selected_live = resolve_selected_leg(info, leg)
    tags_by_leg = tag_index(user_id)
    selected = leg_view(selected_leg, now, tf, tags_by_leg)
    live, extra = compute_live_payload(user_id, selected_leg, is_selected_live, now, settings.poll_seconds, tf)
    if selected:
        selected.update(extra)
    settings_dict = settings.model_dump()
    settings_dict["theme"] = display_theme
    settings_dict["show_flightaware"] = show_fa
    settings_dict["show_fr24"] = show_fr24
    day_numbers = _assign_trip_day_numbers(info.all_legs)
    # Over the WHOLE schedule, so a layover straddling the past/upcoming
    # split still gets a label. See overnight_index().
    overnights = overnight_index(info.all_legs)
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
        "flight_groups": build_flight_list(info, day_numbers, now, tf,
                                           tags_by_leg, overnights),
        "past_count": len(info.past),
        "settings": settings_dict,
        "poll_ms": max(10, settings.poll_seconds) * 1000,
        "is_pilot": pilot is not None,
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
        is_pilot=False, is_admin=False, post_to="/viewer-settings",
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
async def calendar_page(request: Request):
    pilot = current_pilot(request)
    viewer_uid = None if pilot else current_viewer_user_id(request)
    user_id = pilot["id"] if pilot else viewer_uid
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    settings = load_settings(user_id)
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
    by_date = {}
    for leg in legs:
        by_date.setdefault(leg.date, []).append(leg)
    trips = build_trip_spans(legs, settings.time_format)

    def trip_for_day(d):
        for trip in trips:
            if trip["start_date"] <= d <= trip["end_date"]:
                return trip
        return None

    # Only the months that actually have at least one flight — no
    # prev/next browsing needed since nothing else has anything to show.
    months_with_data = sorted({(l.date.year, l.date.month) for l in legs})
    if not months_with_data:
        months_with_data = [(today.year, today.month)]

    cal = cal_module.Calendar(firstweekday=6)  # weeks start Sunday
    month_blocks = []
    for year, month in months_with_data:
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
                "legs": [leg_view(l, now, settings.time_format, tags_by_leg) for l in day_legs],
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
        settings=settings.model_dump(),
        is_pilot=pilot is not None,
    ))


# ---------------------------------------------------------------------------
# Admin (schedule) — pilot only
# ---------------------------------------------------------------------------

@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot

    settings = load_settings(pilot["id"])
    info = get_current_info(pilot["id"])
    upcoming_legs = ([info.current] if info.current else []) + list(info.upcoming)
    past_legs = info.past

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
    template = jinja_env.get_template("admin.html")
    return HTMLResponse(template.render(
        request=request, upcoming_rows=upcoming_rows, past_rows=past_rows,
        past_count=len(past_legs), count=len(upcoming_legs) + len(past_legs),
        settings=settings.model_dump(), share_code=pilot["share_code"],
    ))


@app.post("/admin/import")
async def admin_import(request: Request, text: str = Form(...)):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    legs = parse_schedule_text(text)
    if not legs:
        # Nothing valid parsed — nothing to review, just go back.
        return RedirectResponse(url="/admin", status_code=303)
    apply_gap_trip_starts(legs)
    settings = load_settings(pilot["id"])
    review_legs = build_review_legs(legs, settings.time_format)
    template = jinja_env.get_template("import_review.html")
    return HTMLResponse(template.render(request=request, legs=review_legs, settings=settings.model_dump()))


@app.post("/admin/import/confirm")
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

    save_schedule(pilot["id"], legs)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete/{leg_id}")
async def admin_delete(request: Request, leg_id: str):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    delete_leg(pilot["id"], leg_id)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/regenerate-code")
async def admin_regenerate_code(request: Request):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    regenerate_share_code(pilot["id"])
    return RedirectResponse(url="/admin", status_code=303)


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
           "is_pilot": True, "post_to": "/settings",
           "pilot_id": pilot["id"], "aeroapi_stats": budget_state(pilot["id"]),
           "poller": poller_status(s.time_format)}
    if pilot["is_admin"]:
        ctx["all_users"] = list_all_users()
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
    )
    save_settings(pilot["id"], s)
    template = jinja_env.get_template("settings.html")
    ctx = {"request": request, "s": s, "saved": True, "is_admin": bool(pilot["is_admin"]),
           "is_pilot": True, "post_to": "/settings",
           "pilot_id": pilot["id"], "aeroapi_stats": budget_state(pilot["id"]),
           "poller": poller_status(s.time_format)}
    if pilot["is_admin"]:
        ctx["all_users"] = list_all_users()
    return HTMLResponse(template.render(**ctx))


@app.post("/settings/users/delete/{user_id}")
async def settings_delete_user(request: Request, user_id: int):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    if not pilot["is_admin"]:
        return RedirectResponse(url="/settings", status_code=303)
    if user_id == pilot["id"]:
        # Refuse to let an admin delete their own account from this panel —
        # avoids locking yourself out with no one left to fix it.
        return RedirectResponse(url="/settings", status_code=303)
    delete_user(user_id)
    return RedirectResponse(url="/settings", status_code=303)


# ---------------------------------------------------------------------------
# JSON API — pilot or viewer, same rules as the tracker page
# ---------------------------------------------------------------------------

@app.get("/api/current")
async def api_current(request: Request):
    pilot = current_pilot(request)
    viewer_uid = None if pilot else current_viewer_user_id(request)
    user_id = pilot["id"] if pilot else viewer_uid
    if not user_id:
        return {"error": "not authenticated"}
    info = get_current_info(user_id)
    return info.model_dump(mode="json")


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
    selected_leg, is_selected_live = resolve_selected_leg(info, leg)
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

    selected_leg, is_selected_live = resolve_selected_leg(info, leg_id)
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
        "current": view,
    }
