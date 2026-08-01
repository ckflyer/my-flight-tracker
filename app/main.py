from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pathlib import Path
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from typing import Optional
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .schedule import import_from_text, load_schedule, get_current_info, delete_leg
from .opensky import live_summary
from .models import FlightLeg
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

BASE = Path(__file__).resolve().parent.parent
jinja_env = Environment(
    loader=FileSystemLoader(str(BASE / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
)

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
async def viewer(request: Request):
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
    current = leg_view(info.current, now, tf)
    live = None
    if info.current:
        dep_utc = info.current.dep_datetime_utc()
        arr_utc = info.current.arr_datetime_utc()
        # Poll from 20 min before scheduled departure through 3 hours past
        # scheduled arrival — wide enough to catch taxi-out early and keep
        # tracking through a real delay, capped so we don't poll forever.
        should_poll = (
            dep_utc and arr_utc
            and now >= dep_utc - timedelta(minutes=20)
            and now <= arr_utc + timedelta(hours=3)
        )
        if should_poll:
            live = live_summary(info.current.callsign)
        if current:
            history = get_position_history(user_id, info.current.id)
            current["progress_pct"] = compute_progress(info.current, live, now)
            current["ete"] = compute_ete(info.current, live, now)
            current["distance_nm"] = compute_distance_remaining_nm(info.current, live)
            current["breadcrumb"] = []
            current["aircraft"] = None
            if live and live.get("lat") is not None and live.get("lon") is not None:
                note_aircraft_seen(live.get("icao24"))
                record_position(user_id, info.current.id, live["lat"], live["lon"], now, live.get("on_ground"))
                current["breadcrumb"] = get_breadcrumb(user_id, info.current.id)
                current["aircraft"] = get_aircraft_info(live.get("icao24"))
                history = get_position_history(user_id, info.current.id)  # include the point just recorded
            current["status"] = compute_phase(info.current, live, history, now, settings.poll_seconds)
    day_numbers = _assign_trip_day_numbers(info.all_legs)
    ctx = {
        "request": request,
        "current": current,
        "live": live,
        "upcoming_groups": group_legs_by_day(info.upcoming, day_numbers, now, tf),
        "past_groups": group_legs_by_day(info.past, day_numbers, now, tf),
        "past_count": len(info.past),
        "settings": settings.model_dump(),
        "poll_ms": max(20, settings.poll_seconds) * 1000,
        "is_pilot": pilot is not None,
    }
    template = jinja_env.get_template("viewer.html")
    return HTMLResponse(template.render(**ctx))


# ---------------------------------------------------------------------------
# Admin (schedule) — pilot only
# ---------------------------------------------------------------------------

@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot

    settings = load_settings(pilot["id"])
    legs = load_schedule(pilot["id"])
    rows = []
    for leg in legs:
        rows.append({
            "id": leg.id,
            "date": str(leg.date),
            "callsign": leg.callsign,
            "route": f"{leg.origin} → {leg.destination}",
            "dep": fmt_local(leg, "dep", settings.time_format),
            "arr": fmt_local(leg, "arr", settings.time_format),
        })
    template = jinja_env.get_template("admin.html")
    return HTMLResponse(template.render(
        request=request, rows=rows, count=len(legs), settings=settings.model_dump(),
        share_code=pilot["share_code"],
    ))


@app.post("/admin/import")
async def admin_import(request: Request, text: str = Form(...)):
    pilot = require_pilot(request)
    if isinstance(pilot, RedirectResponse):
        return pilot
    import_from_text(pilot["id"], text, replace=True)
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
