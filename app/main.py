from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pathlib import Path
from datetime import datetime, date, timedelta, time as dtime
from zoneinfo import ZoneInfo
from typing import Optional
import calendar as cal_module
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .schedule import import_from_text, load_schedule, get_current_info, delete_leg, save_schedule
from .opensky import live_summary
from .models import FlightLeg
from .parser import parse_schedule_text
from .airports import enrich_leg
from .settings import load_settings, save_settings, apply_opensky_env, AppSettings
from .aircraft import note_aircraft_seen, get_aircraft_info
from .track import (
    record_position, get_breadcrumb, compute_progress, compute_ete,
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

app = FastAPI(title="Pilot Tracker")
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

def fmt_local(leg: FlightLeg, which: str = "dep", time_format: str = "24") -> str:
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
    if not info:
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
    request.session["_pending_recovery_next"] = "/opensky-guide?first=1"
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
    request.session["_pending_recovery_next"] = "/opensky-guide?first=1"
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


@app.get("/opensky-guide", response_class=HTMLResponse)
async def opensky_guide(request: Request, first: str = ""):
    template = jinja_env.get_template("opensky_guide.html")
    return HTMLResponse(template.render(request=request, show_skip=True, skip_url="/admin"))


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

@app.get("/", response_class=HTMLResponse)
async def viewer(request: Request, leg: Optional[str] = None):
    pilot = current_pilot(request)
    viewer_uid = None if pilot else current_viewer_user_id(request)
    user_id = pilot["id"] if pilot else viewer_uid
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    settings = load_settings(user_id)
    apply_opensky_env(settings)
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

    # Which flight is the map/collapsed card showing? Default: the genuinely
    # active flight if there is one, else the next upcoming one, else the
    # most recent past one. A ?leg= param (from tapping a flight in the
    # list) overrides that, as long as it's a real leg on this schedule.
    selected_leg = info.current or (info.upcoming[0] if info.upcoming else None) or (info.past[-1] if info.past else None)
    if leg:
        match = next((l for l in info.all_legs if l.id == leg), None)
        if match:
            selected_leg = match
    is_selected_live = bool(selected_leg and info.current and selected_leg.id == info.current.id)

    selected = leg_view(selected_leg, now, tf)
    live = None
    if is_selected_live:
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
        if should_poll:
            live = live_summary(selected_leg.callsign)
        if selected:
            history = get_position_history(user_id, selected_leg.id)
            selected["progress_pct"] = compute_progress(selected_leg, live, now)
            selected["ete"] = compute_ete(selected_leg, live, now)
            selected["distance_nm"] = compute_distance_remaining_nm(selected_leg, live)
            selected["breadcrumb"] = []
            selected["aircraft"] = None
            if live and live.get("lat") is not None and live.get("lon") is not None:
                note_aircraft_seen(live.get("icao24"))
                record_position(user_id, selected_leg.id, live["lat"], live["lon"], now, live.get("on_ground"))
                selected["breadcrumb"] = get_breadcrumb(user_id, selected_leg.id)
                selected["aircraft"] = get_aircraft_info(live.get("icao24"))
                history = get_position_history(user_id, selected_leg.id)  # include the point just recorded
            selected["status"] = compute_phase(selected_leg, live, history, now, settings.poll_seconds)
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
        "upcoming_groups": group_legs_by_day(info.upcoming, day_numbers, now, tf),
        "past_groups": group_legs_by_day(info.past, day_numbers, now, tf),
        "past_count": len(info.past),
        "settings": settings_dict,
        "poll_ms": max(20, settings.poll_seconds) * 1000,
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
            agenda.append({
                "iso": d.isoformat(),
                "label": d.strftime("%A, %B %d").replace(" 0", " "),
                "is_today": d == today,
                "legs": [leg_view(l, now, settings.time_format) for l in day_legs],
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
    ctx = {"request": request, "s": s, "saved": False, "is_admin": bool(pilot["is_admin"]), "pilot_id": pilot["id"]}
    if pilot["is_admin"]:
        ctx["all_users"] = list_all_users()
    return HTMLResponse(template.render(**ctx))


@app.post("/settings")
async def settings_save(
    request: Request,
    opensky_client_id: str = Form(""),
    opensky_client_secret: str = Form(""),
    time_format: str = Form("24"),
    theme: str = Form("dark"),
    poll_seconds: int = Form(45),
    show_flightaware: Optional[str] = Form(None),
    show_fr24: Optional[str] = Form(None),
):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    s = AppSettings(
        opensky_client_id=opensky_client_id.strip(),
        opensky_client_secret=opensky_client_secret.strip(),
        time_format="12" if time_format == "12" else "24",
        theme="light" if theme == "light" else "dark",
        poll_seconds=max(20, min(300, int(poll_seconds))),
        show_flightaware=show_flightaware is not None,
        show_fr24=show_fr24 is not None,
    )
    save_settings(pilot["id"], s)
    apply_opensky_env(s)
    template = jinja_env.get_template("settings.html")
    ctx = {"request": request, "s": s, "saved": True, "is_admin": bool(pilot["is_admin"]), "pilot_id": pilot["id"]}
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
