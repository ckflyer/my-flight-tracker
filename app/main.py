from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, date, timedelta, time as dtime
from zoneinfo import ZoneInfo
from typing import Optional
import calendar as cal_module
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .schedule import load_schedule, get_current_info, delete_leg, save_schedule
from .livesource import live_state_for_leg
from .enrichment import (get_enrichment, derive_status, departure_delay,
                         arrival_delay, gate_info, diversion_info,
                         budget_state)
from .closure import get_closeout
from .models import FlightLeg
from .parser import parse_schedule_text
from .airports import enrich_leg
from .settings import load_settings, save_settings, AppSettings
from .track import (
    record_position, get_breadcrumb, compute_progress, compute_ete, last_tracked_at,
    compute_distance_remaining_nm, get_position_history, compute_phase,
)
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

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Record tracks for active flights even with nobody watching.

    The container runs a single uvicorn worker, so this is one poller per
    deployment. If workers are ever added, this would start one per worker
    and they'd poll the same flights redundantly — the shared cache in
    livesource would absorb most of it, but the right fix then is to move
    this to a separate process.

    Was `@app.on_event("startup")`, which FastAPI deprecated; same
    behaviour, no warning on boot.
    """
    from .poller import start as start_poller
    start_poller()
    yield


app = FastAPI(title="Pilot Tracker", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
app.add_middleware(
    SessionMiddleware,
    secret_key=get_or_create_secret_key(),
    session_cookie="pt_session",
    max_age=60 * 60 * 24 * 365,  # a year — sessions are meant to be persistent
    same_site="lax",
)


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


def leg_view(leg: Optional[FlightLeg], now: datetime, time_format: str = "24") -> Optional[dict]:
    if not leg:
        return None
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
        "status": leg.status_at(now),
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


def group_legs_by_day(legs: list, day_numbers: dict, now: datetime, time_format: str = "24") -> list:
    """Groups legs by calendar date, labeled 'Day N - March 27' where N
    resets to 1 at each trip boundary (a blank line in the pasted FFDO —
    see parser.py). Trip boundaries are explicit and pilot-controlled, not
    guessed from gap length, so a real 30+ hour layover mid-trip still
    shows correctly while a multi-day gap *between* two separate trips
    (e.g. days off at home) doesn't get mislabeled as one.

    Between consecutive day-groups within the same trip, computes the
    overnight duration and destination city using the duty-day definition:
    duty ends 15 minutes after block-in, starts 45 minutes before
    block-out. No overnight is shown across a trip boundary — that gap is
    just time off, not a layover.

    day_numbers should come from _assign_trip_day_numbers(info.all_legs) —
    computed once over the whole schedule, not per past/upcoming list, so
    numbering stays continuous across a trip that's partly already flown.
    """
    if not legs:
        return []

    day_buckets = []
    current_date = None
    for leg in legs:
        starts_new_day = leg.date != current_date
        if starts_new_day:
            day_buckets.append({"date": leg.date, "legs": [leg], "trip_start": leg.trip_start})
            current_date = leg.date
        else:
            day_buckets[-1]["legs"].append(leg)
            if leg.trip_start:
                day_buckets[-1]["trip_start"] = True

    groups = []
    for i, bucket in enumerate(day_buckets):
        day_legs = bucket["legs"]
        trip_day_num = day_numbers.get(bucket["date"], 1)
        date_label = bucket["date"].strftime("%B %d").replace(" 0", " ")
        group = {
            "date_label": f"Day {trip_day_num} - {date_label}",
            "legs": [leg_view(l, now, time_format) for l in day_legs],
            "overnight": None,
            "trip_start": bucket["trip_start"],
        }
        if i < len(day_buckets) - 1:
            next_bucket = day_buckets[i + 1]
            if not next_bucket["trip_start"]:
                last_leg = day_legs[-1]
                next_leg = next_bucket["legs"][0]
                last_arr_utc = last_leg.arr_datetime_utc()
                next_dep_utc = next_leg.dep_datetime_utc()
                if last_arr_utc and next_dep_utc:
                    duty_ends = last_arr_utc + timedelta(minutes=15)
                    duty_starts = next_dep_utc - timedelta(minutes=45)
                    gap_seconds = (duty_starts - duty_ends).total_seconds()
                    if gap_seconds > 0:
                        hours = int(gap_seconds // 3600)
                        minutes = int((gap_seconds % 3600) // 60)
                        city = (last_leg.dest_info.city if last_leg.dest_info else last_leg.destination)
                        group["overnight"] = {
                            "duration": f"{hours}h {minutes:02d}m",
                            "city": city,
                        }
        groups.append(group)
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
            "dep": fmt_local(leg, "dep", time_format),
            "arr": fmt_local(leg, "arr", time_format),
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
        return HTMLResponse(template.render(request=request, error="Too many attempts. Try again in a few minutes."), status_code=429)
    user = get_user_by_username(username)
    if not user or not verify_password(password, user["password_hash"]):
        template = jinja_env.get_template("login.html")
        return HTMLResponse(template.render(request=request, error="Incorrect username or password."))
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


def _finish_payload(user_id: int, selected_leg, live, extra, now: datetime,
                    time_format: str, is_live: bool):
    """Closeout record, airline enrichment, and the final status word.

    Split out of compute_live_payload so BOTH paths run it — the live leg
    and the selected-but-not-live one. Everything here reads from the
    database and never spends an AeroAPI query, which is what makes it
    safe to run on a page render for any leg the user taps.

    `is_live` gates only the things that are meaningless without a live
    aircraft: recomputing time-to-go and progress. A past flight keeps its
    stored actual times, gates and closeout record.
    """
    # Read-only: page renders must never spend one of the pilot's queries.
    # A closed leg reports its FROZEN record. Nothing recomputes, so the
    # numbers a past flight shows never drift.
    closeout = get_closeout(user_id, selected_leg.id)
    if closeout:
        extra["closed"] = True
        # A closed leg's status comes from the closure record, so there is
        # exactly one definition of "arrived" rather than the phase machine
        # having its own. Diverted and Cancelled are API-only facts.
        if closeout.get("cancelled"):
            extra["final_status"] = "Cancelled"
        elif closeout.get("diverted"):
            extra["final_status"] = "Diverted"
        else:
            extra["final_status"] = "Arrived"
        extra["closed_by"] = closeout.get("closed_by")
        extra["arrival_source"] = closeout.get("arrival_source")
        extra["closeout"] = closeout

    enr = get_enrichment(user_id, selected_leg.id)
    if enr:
        extra["status"] = derive_status(enr, extra.get("status"))
        # Departure and arrival are answered separately: "is he getting
        # out?" and "when does he get there?" are different questions, and
        # a late pushback doesn't always become a late arrival.
        status_now = extra["status"]
        # Our own observed gate stop, used when the airline hasn't published
        # actual_in yet — a fact beats a stale forecast.
        try:
            from .flightmatch import observed_gate_in
            obs_in = observed_gate_in(selected_leg)
        except Exception:
            obs_in = None
        extra["dep_delay"] = departure_delay(enr, selected_leg, time_format, status=status_now)
        extra["arr_delay"] = arrival_delay(enr, selected_leg, time_format,
                                           status=status_now, observed_in=obs_in)

        # Status is already final here, so this is a reliable read.
        airborne = bool(enr.get("actual_off")) or status_now in (
            "In Air", "Landing", "Taxi-in", "Arrived", "Diverting")

        # Recompute time-to-go against the REVISED arrival, so a known delay
        # is reflected instead of counting down to a schedule that's already
        # been superseded.
        revised_arr = None
        for key in ("actual_in", "estimated_in", "actual_on", "estimated_on"):
            parsed = _parse_iso(enr.get(key))
            if parsed:
                revised_arr = parsed
                break
        # Time-to-go and progress only mean anything while the flight is
        # actually the live one. A past leg that kept recomputing these
        # would show a countdown to an arrival that already happened.
        if revised_arr and is_live:
            extra["ete"] = compute_ete(selected_leg, live, now, arr_override=revised_arr)
            if airborne:
                extra["progress_pct"] = compute_progress(
                    selected_leg, live, now, departed=True,
                    dep_override=_parse_iso(enr.get("actual_off")) or _parse_iso(enr.get("actual_out")),
                    arr_override=revised_arr)
        # THE BUG THIS FIXES: the card used to print the FFDO scheduled
        # time and then a note saying "18 min early" beside it, so the two
        # never agreed. When there's an actual or estimated time, that is
        # what gets shown, with the scheduled time kept as "was ...".
        if extra["dep_delay"] and extra["dep_delay"].get("time"):
            extra["dep_shown"] = extra["dep_delay"]["time"]
        if extra["arr_delay"] and extra["arr_delay"].get("time"):
            extra["arr_shown"] = extra["arr_delay"]["time"]
        extra["gates"] = gate_info(enr)
        extra["diversion"] = diversion_info(enr, selected_leg.destination)
        extra["ooi"] = {
            "out": enr.get("actual_out"), "off": enr.get("actual_off"),
            "on": enr.get("actual_on"), "in": enr.get("actual_in"),
        }
        extra["enriched"] = True
        # So it's visible how fresh the airline data is, and how rarely it
        # actually needs fetching.
        fetched = enr.get("_fetched_at")
        if fetched:
            try:
                age = (now - datetime.fromisoformat(fetched)).total_seconds() / 60
                extra["enriched_at"] = ("just now" if age < 1.5
                                        else f"{int(age)} min ago" if age < 90
                                        else f"{int(age // 60)}h ago")
            except Exception:
                pass

    # Departure guard, applied last so it sees the final status. A flight
    # that isn't demonstrably airborne shows no progress bar and no
    # distance-to-go, rather than a figure derived from a schedule.
    _airborne = (
        (live is not None and live.get("on_ground") is False)
        or extra.get("status") in ("In Air", "Landing", "Taxi-in", "Arrived", "Diverting")
    )
    if not _airborne:
        extra["progress_pct"] = None
        extra["distance_nm"] = None

    # Closure has the final word on status.
    if extra.get("final_status"):
        extra["status"] = extra["final_status"]
    return live, extra


def compute_live_payload(user_id: int, selected_leg, is_selected_live: bool, now: datetime, time_format: str = "24"):
    """Live ADS-B + progress/ETE/breadcrumb/aircraft/phase for the selected
    leg, if it's genuinely the active one. Shared by the full page render
    and the lightweight polling endpoint so the two never drift apart."""
    live = None
    extra = {"progress_pct": None, "ete": None, "distance_nm": None, "breadcrumb": [], "aircraft": None, "status": None}
    if not selected_leg:
        return live, extra
    if not is_selected_live:
        extra["status"] = selected_leg.status_at(now)
        # A past (or not-yet-active) leg has no live data, but it may well
        # have a stored track from when it WAS flying. Hand that back so
        # the map can draw the real flown path.
        extra["breadcrumb"] = get_breadcrumb(selected_leg.id)
        # Deliberately NOT returning here. This used to return early, which
        # meant the closeout and enrichment blocks below never ran for a
        # leg that wasn't the live one — so the moment a flight aged out of
        # the 3-hour grace window, its actual times, gates and closeout
        # record vanished from the card. The data was on disk the whole
        # time; nothing ever read it. It also blanked the not-yet-departed
        # leg, which is precisely what the T-30 first look exists to fill.
        return _finish_payload(user_id, selected_leg, live, extra, now,
                               time_format, is_live=False)

    dep_utc = selected_leg.dep_datetime_utc()
    arr_utc = selected_leg.arr_datetime_utc()
    # Poll from 20 min before scheduled departure through 3 hours past
    # scheduled arrival — wide enough to catch taxi-out early and keep
    # tracking through a real delay, capped so we don't poll forever.
    should_poll = (
        dep_utc and arr_utc
        and now >= dep_utc - timedelta(minutes=20)
        and now <= arr_utc + timedelta(hours=3)
    )
    # History is read BEFORE the live lookup so the turn-flight guard can
    # see whether this leg has already finished — if it has, whatever is
    # broadcasting the callsign now is the next flight, not this one.
    history = get_position_history(selected_leg.id)
    if should_poll:
        live = live_state_for_leg(selected_leg, now, history)
    extra["breadcrumb"] = get_breadcrumb(selected_leg.id)
    extra["progress_pct"] = compute_progress(selected_leg, live, now)  # provisional; revised below
    extra["ete"] = compute_ete(selected_leg, live, now)
    extra["distance_nm"] = compute_distance_remaining_nm(selected_leg, live)

    if live and live.get("lat") is not None and live.get("lon") is not None:
        record_position(selected_leg.id, live["lat"], live["lon"], now, live.get("on_ground"))
        extra["breadcrumb"] = get_breadcrumb(selected_leg.id)
        # Tail number and type ride along with the live position now, so
        # there's no separate lookup to wait on and nothing to cache.
        extra["aircraft"] = {
            "registration": live.get("registration"),
            "display_type": live.get("aircraft_type"),
        } if (live.get("registration") or live.get("aircraft_type")) else None
        history = get_position_history(selected_leg.id)  # include the point just recorded
    # ADS-B phase FIRST, then enrichment refines it.
    #
    # This assignment used to happen at the very END of the function, after
    # the enrichment block had already set extra["status"] — so it silently
    # overwrote every OOOI-derived status, including "Delayed". A flight
    # sitting at the gate two hours late still read "Departing" because the
    # airline's own view of it was computed and then thrown away.
    extra["status"] = compute_phase(selected_leg, live, history, now)
    # When the aircraft isn't being tracked, say so and say when it last
    # was, instead of guessing a phase.
    if extra["status"] == "Unknown":
        seen = last_tracked_at(history)
        if seen:
            gap = (now - seen).total_seconds() / 60
            if gap < 60:
                extra["last_tracked"] = f"{int(gap)} min ago"
            else:
                tz = getattr(selected_leg.dest_info, "timezone", None)
                extra["last_tracked"] = _fmt_utc_local(seen, tz, time_format)

    return _finish_payload(user_id, selected_leg, live, extra, now,
                           time_format, is_live=True)


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
    selected = leg_view(selected_leg, now, tf)
    live, extra = compute_live_payload(user_id, selected_leg, is_selected_live, now, tf)
    if selected:
        selected.update(extra)
    settings_dict = settings.model_dump()
    settings_dict["theme"] = display_theme
    settings_dict["show_flightaware"] = show_fa
    settings_dict["show_fr24"] = show_fr24
    day_numbers = _assign_trip_day_numbers(info.all_legs)
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
        "upcoming_groups": group_legs_by_day(info.upcoming, day_numbers, now, tf),
        "past_groups": group_legs_by_day(info.past, day_numbers, now, tf),
        "past_count": len(info.past),
        "settings": settings_dict,
        "poll_ms": max(10, settings.poll_seconds) * 1000,
        "is_pilot": pilot is not None,
    }
    template = jinja_env.get_template("viewer.html")
    return HTMLResponse(template.render(**ctx))


@app.get("/viewer-settings", response_class=HTMLResponse)
async def viewer_settings_get(request: Request):
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
    show_fa = request.cookies.get("pt_viewer_show_fa", "1") == "1"
    show_fr24 = request.cookies.get("pt_viewer_show_fr24", "1") == "1"
    template = jinja_env.get_template("viewer_settings.html")
    return HTMLResponse(template.render(request=request, theme=theme, tf=tf, show_fa=show_fa, show_fr24=show_fr24))


@app.post("/viewer-settings")
async def viewer_settings_post(
    request: Request,
    theme: str = Form("dark"),
    time_format: str = Form("24"),
    show_fa: Optional[str] = Form(None),
    show_fr24: Optional[str] = Form(None),
):
    pilot = current_pilot(request)
    viewer_uid = None if pilot else current_viewer_user_id(request)
    if not pilot and not viewer_uid:
        return RedirectResponse(url="/login", status_code=303)
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie("pt_viewer_theme", "light" if theme == "light" else "dark", max_age=60 * 60 * 24 * 365)
    resp.set_cookie("pt_viewer_tf", "12" if time_format == "12" else "24", max_age=60 * 60 * 24 * 365)
    resp.set_cookie("pt_viewer_show_fa", "1" if show_fa is not None else "0", max_age=60 * 60 * 24 * 365)
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
    today = now.date()

    legs = load_schedule(user_id)
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
                "legs": [leg_view(l, now, settings.time_format) for l in day_legs],
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
                "date": str(leg.date),
                "callsign": leg.callsign,
                "route": f"{leg.origin} → {leg.destination}",
                "dep": fmt_local(leg, "dep", settings.time_format),
                "arr": fmt_local(leg, "arr", settings.time_format),
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

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    s = load_settings(pilot["id"])
    template = jinja_env.get_template("settings.html")
    ctx = {"request": request, "s": s, "saved": False, "is_admin": bool(pilot["is_admin"]),
           "pilot_id": pilot["id"], "aeroapi_stats": budget_state(pilot["id"])}
    if pilot["is_admin"]:
        ctx["all_users"] = list_all_users()
    return HTMLResponse(template.render(**ctx))


@app.post("/settings")
async def settings_save(
    request: Request,
    aeroapi_enabled: str = Form(""),
    aeroapi_key: str = Form(""),
    aeroapi_allow_overage: str = Form(""),
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
        aeroapi_allow_overage=bool(aeroapi_allow_overage),
        time_format="12" if time_format == "12" else "24",
        theme="light" if theme == "light" else "dark",
        poll_seconds=max(10, min(300, int(poll_seconds))),
        show_flightaware=show_flightaware is not None,
        show_fr24=show_fr24 is not None,
    )
    save_settings(pilot["id"], s)
    template = jinja_env.get_template("settings.html")
    ctx = {"request": request, "s": s, "saved": True, "is_admin": bool(pilot["is_admin"]),
           "pilot_id": pilot["id"], "aeroapi_stats": budget_state(pilot["id"])}
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
    live, extra = compute_live_payload(user_id, selected_leg, is_selected_live, now, tf)
    return {
        "is_selected_live": is_selected_live,
        # Which leg the app currently considers active. The page compares
        # this to what it's showing so it can switch flights on its own
        # when one ends and the next begins — that used to require the
        # five-minute full-page reload.
        "current_leg_id": info.current.id if info.current else None,
        "live": live,
        "status": extra.get("status"),
        "progress_pct": extra.get("progress_pct"),
        "ete": extra.get("ete"),
        "dep_delay": extra.get("dep_delay"),
        "arr_delay": extra.get("arr_delay"),
        "enriched_at": extra.get("enriched_at"),
        "last_tracked": extra.get("last_tracked"),
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

    view = leg_view(selected_leg, now, tf)
    live, extra = compute_live_payload(user_id, selected_leg, is_selected_live, now, tf)
    if view:
        view.update(extra)
    return {
        "leg_id": selected_leg.id,
        "is_selected_live": is_selected_live,
        "current_leg_id": info.current.id if info.current else None,
        "live": live,
        "current": view,
    }
