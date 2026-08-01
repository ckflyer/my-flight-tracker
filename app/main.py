from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
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

BASE = Path(__file__).resolve().parent.parent
jinja_env = Environment(
    loader=FileSystemLoader(str(BASE / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
)

app = FastAPI(title="Flight Tracker")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

# Load stored OpenSky creds into env on startup
apply_opensky_env()


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


@app.get("/", response_class=HTMLResponse)
async def viewer(request: Request):
    settings = load_settings()
    apply_opensky_env(settings)
    info = get_current_info()
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
            history = get_position_history(info.current.id)
            current["progress_pct"] = compute_progress(info.current, live, now)
            current["ete"] = compute_ete(info.current, live, now)
            current["distance_nm"] = compute_distance_remaining_nm(info.current, live)
            current["breadcrumb"] = []
            current["aircraft"] = None
            if live and live.get("lat") is not None and live.get("lon") is not None:
                note_aircraft_seen(live.get("icao24"))
                record_position(info.current.id, live["lat"], live["lon"], now, live.get("on_ground"))
                current["breadcrumb"] = get_breadcrumb(info.current.id)
                current["aircraft"] = get_aircraft_info(live.get("icao24"))
                history = get_position_history(info.current.id)  # include the point just recorded
            current["status"] = compute_phase(info.current, live, history, now, settings.poll_seconds)
    ctx = {
        "request": request,
        "current": current,
        "live": live,
        "upcoming": [leg_view(l, now, tf) for l in info.upcoming],
        "past": [leg_view(l, now, tf) for l in info.past],
        "settings": settings.model_dump(),
        "poll_ms": max(20, settings.poll_seconds) * 1000,
    }
    template = jinja_env.get_template("viewer.html")
    return HTMLResponse(template.render(**ctx))


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    settings = load_settings()
    legs = load_schedule()
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
    ))


@app.post("/admin/import")
async def admin_import(text: str = Form(...)):
    import_from_text(text, replace=True)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete/{leg_id}")
async def admin_delete(leg_id: str):
    delete_leg(leg_id)
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    s = load_settings()
    template = jinja_env.get_template("settings.html")
    return HTMLResponse(template.render(request=request, s=s, saved=False))


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
    s = AppSettings(
        opensky_client_id=opensky_client_id.strip(),
        opensky_client_secret=opensky_client_secret.strip(),
        time_format="12" if time_format == "12" else "24",
        theme="light" if theme == "light" else "dark",
        poll_seconds=max(20, min(300, int(poll_seconds))),
        show_flightaware=show_flightaware is not None,
        show_fr24=show_fr24 is not None,
    )
    save_settings(s)
    apply_opensky_env(s)
    template = jinja_env.get_template("settings.html")
    return HTMLResponse(template.render(request=request, s=s, saved=True))


@app.get("/api/current")
async def api_current():
    info = get_current_info()
    return info.model_dump(mode="json")
