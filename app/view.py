"""Turning a flight row into what the card shows.

This module is READ-ONLY. It never fetches, never spends a query, and
never writes. That is the whole point of the v5 rebuild: the poller
decides what is true and writes it down, and this just renders the row.
In v4 the equivalent code ran on every page render for every viewer, and
re-derived the status by reconciling three tables — which is where the
ordering bugs lived.

WHICH FIGURE GETS SHOWN
-----------------------
Arrival is taken in this order: the airline's actual gate-in, then OUR OWN
observed gate stop, then the estimate. The airline publishes actual_in
with a lag, so a flight can be parked while the only airline figure
available is an hour-old forecast; presenting that forecast as the
arrival time is how "arrived 4:05, 11 minutes early" appeared for a flight
that blocked in at 4:11.

When a revised time exists, the card shows THAT time with the scheduled
one struck through beneath it. Delta and displayed time are both derived
from minute-truncated values so they can never disagree by a rounding
minute — the card used to print a scheduled time next to a note saying
"18 min early", and the two never matched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from . import tags
from .track import (compute_distance_nm, compute_progress, format_ete,
                    compute_remaining_minutes)

# Zero tolerance, by the pilot's own call: one minute late IS late, and a
# card that prints 5:59 beside a crossed-out 5:57 and calls it "on time"
# is arguing with itself.
ON_TIME_TOLERANCE_MIN = 0


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _col(row, name):
    try:
        return row[name]
    except Exception:
        return None


def _fmt_local(dt: Optional[datetime], tz_name: Optional[str],
               time_format: str = "24", with_zone: bool = True) -> Optional[str]:
    """A UTC instant as a clock time at the airport, with its zone."""
    if dt is None or not tz_name:
        return None
    try:
        local = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        return None
    text = (local.strftime("%I:%M %p").lstrip("0") if time_format == "12"
            else local.strftime("%H:%M"))
    if not with_zone:
        return text
    return f"{text} {local.tzname() or tz_name.split('/')[-1]}"


def _span(minutes: int) -> str:
    amount = abs(minutes)
    return f"{amount // 60}h {amount % 60:02d}m" if amount >= 60 else f"{amount} min"


def _variance(baseline: Optional[datetime], actual, observed, estimate,
              tz_name: Optional[str], time_format: str,
              verb_future: str, verb_past: str,
              settled_override: Optional[bool] = None) -> Optional[Dict[str, Any]]:
    """The "12 min late" note, and which time to print.

    `baseline` is the FFDO time — the pilot's own bid line, not the
    airline's published schedule. Those differ by a couple of minutes
    routinely, and he flies the bid line, so that is what late means here.

    NOTE THIS IS NOT THE DELAYED PILL. The pill needs the airline to have
    actually pushed something (see tags.compute_status). This note is
    honest about the clock regardless, so a flight can read no-pill and
    still say "out 12 min late".
    """
    if baseline is None:
        return None
    actual_dt = _parse(actual)
    observed_dt = _parse(observed)
    estimate_dt = _parse(estimate)
    compare = actual_dt or observed_dt or estimate_dt
    if compare is None:
        return None

    # Truncate BOTH to the minute before doing anything else. The displayed
    # clock drops seconds, so computing the delta from raw instants could
    # round the other way and leave the note disagreeing with the time
    # printed next to it — exactly the mismatch this block exists to fix.
    compare = compare.replace(second=0, microsecond=0)
    baseline = baseline.replace(second=0, microsecond=0)
    minutes = int(round((compare - baseline).total_seconds() / 60))

    # Past tense follows the FLIGHT, not the API. A card reading "Arrived"
    # beside "Arrives 11 min early" is arguing with itself.
    settled = (actual_dt is not None) or (observed_dt is not None)
    if settled_override is not None:
        settled = settled_override

    revised = _fmt_local(compare, tz_name, time_format)
    original = _fmt_local(baseline, tz_name, time_format)
    base = {
        "minutes": minutes,
        "settled": settled,
        "time": revised,
        "original": original,
        "time_short": _fmt_local(compare, tz_name, time_format, with_zone=False),
        "original_short": _fmt_local(baseline, tz_name, time_format, with_zone=False),
        "source": ("airline" if actual_dt else "observed" if observed_dt else "estimated"),
    }
    if abs(minutes) <= ON_TIME_TOLERANCE_MIN:
        base.update({"state": "ontime",
                     "text": f"{verb_past} on time" if settled else "On time"})
        return base
    word = "late" if minutes > 0 else "early"
    base.update({
        "state": word,
        "text": f"{verb_past if settled else verb_future} {_span(minutes)} {word}",
        "short_text": f"{_span(minutes)} {word}",
    })
    return base


def _time_line(variance: Optional[Dict[str, Any]], baseline: Optional[datetime],
               tz_name: Optional[str], time_format: str) -> Optional[Dict[str, Any]]:
    """One row's worth of "when, and how far off" — time plus variance.

    Replaces the old three-row block (Departure note / Arrival note /
    Scheduled pair). Those split one fact across three lines and still made
    you do the arithmetic: the note said "28 min late" on one row while the
    time it was late RELATIVE TO sat two rows below, next to an unrelated
    one. Two rows now, each self-contained:

        Departure   12:39 CDT
                    12 min late  ·  was 12:27 CDT

    Always returns something when a scheduled time exists, so an unflown
    leg still shows its times rather than nothing at all — the empty-field
    hiding would otherwise wipe a future flight's block entirely now that
    the Scheduled row is gone.
    """
    if variance:
        revised, original = variance.get("time"), variance.get("original")
        return {
            "time": revised,
            # Only when it actually moved. Striking through a time
            # identical to the one above it is noise.
            "was": original if (original and original != revised) else None,
            "note": (variance.get("short_text")
                     or ("on time" if variance.get("state") == "ontime" else None)),
            "state": variance.get("state"),
        }
    shown = _fmt_local(baseline, tz_name, time_format)
    if not shown:
        return None
    return {"time": shown, "was": None, "note": None, "state": "scheduled"}


def build(row, leg, now: datetime, time_format: str = "24",
          include_breadcrumb: bool = True) -> Dict[str, Any]:
    """Everything the card needs, from the row and nothing else."""
    if row is None:
        return {"phase_tag": None, "status_tag": None, "breadcrumb": []}

    closed = bool(_col(row, "closed"))
    cancelled = bool(_col(row, "cancelled"))
    status_tag = _col(row, "status_tag")
    phase_tag = _col(row, "phase_tag") or tags.PHASE_SCHEDULED
    # A closed leg reads Arrived whatever the stored phase says. Closure
    # writes this too; the guard is here as well so a row closed by an
    # older build can never render as "Scheduled" on a flight that
    # finished yesterday.
    if closed:
        phase_tag = tags.PHASE_ARRIVED
    # A cancelled flight hides the phase pill entirely — the aeroplane
    # isn't doing anything, so "Scheduled" beside "Cancelled" is noise.
    if cancelled or status_tag == tags.STATUS_CANCELLED:
        phase_tag = None

    o_tz = getattr(leg.origin_info, "timezone", None) if leg.origin_info else None
    d_tz = getattr(leg.dest_info, "timezone", None) if leg.dest_info else None

    departed_flag = phase_tag in (tags.PHASE_IN_AIR, tags.PHASE_LANDING,
                                  tags.PHASE_TAXI_IN, tags.PHASE_ARRIVED)
    dep_delay = _variance(
        leg.dep_datetime_utc(), _col(row, "out_actual_api"), _col(row, "out_observed"),
        _col(row, "out_estimated"), o_tz, time_format, "Departing", "Departed",
        settled_override=True if departed_flag else None)
    arr_delay = _variance(
        leg.arr_datetime_utc(), _col(row, "in_actual_api"), _col(row, "in_observed"),
        _col(row, "in_estimated"), d_tz, time_format, "Arrives", "Arrived",
        settled_override=True if phase_tag == tags.PHASE_ARRIVED or closed else None)
    if cancelled:
        arr_delay = {"state": "cancelled", "minutes": 0, "settled": True,
                     "time": None, "original": None, "text": "Cancelled"}

    gates = {
        "origin_gate": _col(row, "gate_origin"),
        "origin_terminal": _col(row, "terminal_origin"),
        "dest_gate": _col(row, "gate_destination"),
        "dest_terminal": _col(row, "terminal_destination"),
        "baggage": _col(row, "baggage_claim"),
    }
    if not any(gates.values()):
        gates = None

    diversion = None
    if _col(row, "diverted"):
        actual_dest = _col(row, "destination_actual")
        diversion = {
            "diverted_to": (actual_dest if actual_dest
                            and actual_dest != (leg.destination or "").upper() else None),
            "scheduled": leg.destination,
        }

    reg = _col(row, "tail_api") or _col(row, "tail_adsb")
    ac_type = _col(row, "aircraft_type") or _col(row, "type_code")
    aircraft = ({"registration": reg, "display_type": ac_type}
                if (reg or ac_type) else None)

    lat, lon = _col(row, "last_lat"), _col(row, "last_lon")
    live = None
    if lat is not None and lon is not None and not closed:
        live = {
            "lat": lat, "lon": lon,
            "on_ground": (None if _col(row, "last_on_ground") is None
                          else bool(_col(row, "last_on_ground"))),
            "altitude_ft": _col(row, "last_altitude_ft"),
            "speed_kts": _col(row, "last_speed_kts"),
            "track": _col(row, "last_track"),
            "registration": reg,
            "aircraft_type": ac_type,
            "position_age_s": _col(row, "last_fix_age_s"),
        }

    dep_line = _time_line(dep_delay, leg.dep_datetime_utc(), o_tz, time_format)
    arr_line = _time_line(arr_delay, leg.arr_datetime_utc(), d_tz, time_format)
    if cancelled and arr_line:
        arr_line = dict(arr_line, note="cancelled", state="cancelled")

    payload = {
        "dep_line": dep_line,
        "arr_line": arr_line,
        "phase_tag": phase_tag,
        "status_tag": status_tag,
        # Kept so anything still reading `status` gets the more urgent of
        # the two, which is what a single badge would have shown.
        "status": status_tag or phase_tag,
        # One small-print slot under the card. Loss of signal is the more
        # urgent thing to say; otherwise, if we're sitting in the closeout
        # loop, say so rather than looking frozen for no visible reason.
        "signal_note": None if closed else (
            tags.signal_note(row, now)
            or ("waiting on airline gate-in"
                if (_col(row, "closeout_tries") and not _col(row, "in_actual_api"))
                else None)),
        "progress_pct": _col(row, "progress_pct"),
        "ete": format_ete(_col(row, "ete_min")),
        "distance_nm": _col(row, "distance_nm"),
        "dep_delay": dep_delay,
        "arr_delay": arr_delay,
        "dep_shown": (dep_delay or {}).get("time"),
        "arr_shown": (arr_delay or {}).get("time"),
        "gates": gates,
        "diversion": diversion,
        "aircraft": aircraft,
        "closed": closed,
        "closed_by": _col(row, "closed_by"),
        "arrival_source": _col(row, "arrival_source"),
        "enriched": bool(_col(row, "last_api_query_at")),
        "enriched_at": _ago_text(_parse(_col(row, "last_api_query_at")), now),
        # The same instant as an ISO string, so the page can recompute
        # "23 min ago" on a timer instead of freezing at whatever it said
        # when the HTML was generated. The server text above stays as the
        # value rendered on first paint and as the fallback if the client
        # can't parse it.
        "enriched_at_iso": (lambda d: d.isoformat() if d else None)(
            _parse(_col(row, "last_api_query_at"))),
        "last_signal_iso": (lambda d: d.isoformat() if d else None)(
            _parse(_col(row, "last_signal_at"))),
        "last_tracked": _ago_text(_parse(_col(row, "last_signal_at")), now),
        "waiting_on_airline": bool(
            not closed and _col(row, "closeout_tries")
            and not _col(row, "in_actual_api")),
        "live": live,
    }
    if include_breadcrumb:
        from .track import get_breadcrumb
        payload["breadcrumb"] = get_breadcrumb(leg.id)
    return payload


def _ago_text(when: Optional[datetime], now: datetime) -> Optional[str]:
    if when is None:
        return None
    mins = (now - when).total_seconds() / 60
    if mins < 1.5:
        return "just now"
    if mins < 90:
        return f"{int(mins)} min ago"
    return f"{int(mins // 60)}h ago"


def recompute_derived(row, leg, now: datetime) -> Dict[str, Any]:
    """Progress, distance and time-to-go. Written by the poller.

    Progress is pinned to zero until there is EVIDENCE the aircraft left —
    a live fix showing not-on-ground, or a phase of In air or later.
    Without that guard the old elapsed-time fallback measured the clock
    against the SCHEDULE, so a flight still at the gate showed 27% en
    route and, once past its scheduled arrival, 100%.
    """
    lat, lon = _col(row, "last_lat"), _col(row, "last_lon")
    phase = _col(row, "phase_tag")
    departed = (
        _col(row, "last_on_ground") == 0
        or phase in (tags.PHASE_IN_AIR, tags.PHASE_LANDING,
                     tags.PHASE_TAXI_IN, tags.PHASE_ARRIVED)
        or bool(_col(row, "off_actual_api"))
    )
    # No live fix means no figures at all, and the strip draws empty.
    # Better than a number derived from the clock.
    stale = tags.signal_note(row, now) is not None
    if not departed or stale:
        lat = lon = None

    # progress / distance / ETE are ALL position-derived, or absent. The
    # revised arrival that used to feed an ETE fallback here is still read
    # from the row further up and rendered on the Arrival line, where it is
    # labelled as the airline's estimate rather than dressed as a
    # measurement.
    remaining = compute_remaining_minutes(
        leg, lat, lon, _col(row, "last_speed_kts"))
    return {
        "progress_pct": compute_progress(leg, lat, lon, departed),
        "distance_nm": compute_distance_nm(leg, lat, lon),
        "ete_min": remaining,
    }
