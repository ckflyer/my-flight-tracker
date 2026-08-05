"""When to spend a query, and what to do with the answer.

The pilot's own AeroAPI key pays for this, so the budget rules matter as
much as the data:

  * ONLY the background poller refreshes enrichment. Page renders read the
    cache and never trigger a fetch, so a family member hitting refresh
    fifty times during a delay costs nothing.
  * ADS-B transitions are the primary trigger. Wheels-up, wheels-down and
    stopped-at-the-gate are already detected for free, so a query is spent
    when something actually changed rather than on a timer.
  * A schedule-based fallback covers the case that breaks the above: no
    ADS-B coverage at the outstation means no transitions to react to.
  * Hard caps per leg and a minimum gap between queries, so no bug can run
    up someone's bill.

Budget works out around four to six queries per leg.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional

from .aeroapi import AeroApiError, fetch_leg
import os

from .db import get_connection

# Never query a leg more often than this, whatever the triggers say.
MIN_QUERY_GAP = timedelta(minutes=20)
# How many evenly spaced checks to make between departure and arrival. Any
# that would land inside MIN_QUERY_GAP of the previous query are skipped,
# so a short leg simply uses fewer.
CRUISE_CHECKS = 3
# Absolute ceiling per leg — a runaway loop can't cost more than this.
# Raised from 10 to make room for the closeout pass, which is reserved
# below so a chatty flight can't starve it.
MAX_QUERIES_PER_LEG = 10
# Of that ceiling, held back purely for confirming gate-in, so a leg
# that spent heavily on delays still has queries left to close out.
CLOSEOUT_RESERVE = 2
# Of that ceiling, how many are held back purely for hunting actual_in.
# Held back for CONFIRMING ARRIVAL — closeout, or the no-ADS-B fallback.
# Both answer the same question ("has it blocked in?"), and that question
# is the one that closes the leg, so it can't be starved by earlier
# triggers.
ARRIVAL_RESERVE = 2
# Hard caps on the repeating triggers. Both loop on a timer until they get
# an answer, and when gate-in simply never publishes — which happens — an
# uncapped loop eats the whole per-leg budget waiting for something that
# isn't coming. After these, the backstop closes the leg instead.
MAX_CLOSEOUT_TRIES = 2
MAX_ARRIVAL_FALLBACK_TRIES = 2
# While a flight is stuck on the ground past its departure time, check back
# on this cadence rather than letting the cruise checks fire — they're for
# a flight that's actually flying. Capped, so a long ground delay can't
# drain the leg's budget.
DELAY_WATCH_GAP = timedelta(minutes=30)
MAX_DELAY_WATCH_TRIES = 3
# Once the aircraft is down, how often to ask whether gate-in has posted.
CLOSEOUT_GAP = timedelta(minutes=10)
# And for how long before giving up and letting the backstop close it.
CLOSEOUT_WINDOW = timedelta(minutes=90)

# What a /flights/{ident} call actually costs, per FlightAware's published
# rate. Used only to show spend and to enforce the budget below.
COST_PER_QUERY_USD = float(os.environ.get("AEROAPI_COST_PER_QUERY", "0.005"))
# Hard monthly ceiling. The Personal tier includes $5/month free ($10 if
# you feed ADS-B); defaulting under that means the app can never quietly
# produce a bill. Raise it if you're on a paid tier.
MONTHLY_BUDGET_USD = float(os.environ.get("AEROAPI_MONTHLY_BUDGET", "4.50"))
# How early to take a first look (gate assignment and any pre-departure delay).
PREVIEW_BEFORE_DEP = timedelta(minutes=60)
# If ADS-B has told us nothing by this far past scheduled departure, ask anyway.
SILENT_DEP_FALLBACK = timedelta(minutes=15)
# How long after touchdown to look for gate-in. Often the aircraft is on
# stand within this, which closes the leg in one query instead of several.
AFTER_LANDING = timedelta(minutes=5)
# Fallback for legs with no ADS-B at all: one look after the estimated
# arrival, since without a wheels-down event nothing else would ever fire.
ARRIVAL_FALLBACK = timedelta(minutes=10)


# --------------------------------------------------------------- storage
def get_enrichment(user_id: int, leg_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT payload, fetched_at FROM flight_enrichment WHERE leg_id = ? AND user_id = ?",
            (leg_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        data = json.loads(row["payload"])
        data["_fetched_at"] = row["fetched_at"]
        return data
    except Exception:
        return None


def _store(user_id: int, leg_id: str, payload: Dict[str, Any], now: datetime,
           raw: Optional[Dict[str, Any]] = None) -> None:
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT first_seen FROM flight_enrichment WHERE leg_id = ? AND user_id = ?",
            (leg_id, user_id),
        ).fetchone()
        # Snapshot the first values seen and never overwrite them — that's
        # the only record of the ORIGINAL schedule once an airline amends it.
        first_seen = existing["first_seen"] if existing and existing["first_seen"] else json.dumps(payload)
        conn.execute(
            "INSERT OR REPLACE INTO flight_enrichment "
            "(leg_id, user_id, fetched_at, payload, raw, first_seen) VALUES (?, ?, ?, ?, ?, ?)",
            (leg_id, user_id, now.isoformat(), json.dumps(payload),
             json.dumps(raw) if raw is not None else None, first_seen),
        )
        conn.commit()
    finally:
        conn.close()


def get_first_seen(user_id: int, leg_id: str) -> Optional[Dict[str, Any]]:
    """The earliest snapshot of this leg, before any schedule amendments."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT first_seen FROM flight_enrichment WHERE leg_id = ? AND user_id = ?",
            (leg_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["first_seen"]:
        return None
    try:
        return json.loads(row["first_seen"])
    except Exception:
        return None


def _count_query(user_id: int, now: datetime, billed: int = 1) -> None:
    """Add this call's RESULT SETS to the month's tally.

    Billing is per result set of up to 15 records, so a call can cost more
    than one. Counting calls would under-report.
    """
    period = now.strftime("%Y-%m")
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT aeroapi_period, aeroapi_queries FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row or row["aeroapi_period"] != period:
            conn.execute(
                "UPDATE users SET aeroapi_period = ?, aeroapi_queries = ? WHERE id = ?",
                (period, billed, user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET aeroapi_queries = aeroapi_queries + ? WHERE id = ?",
                (billed, user_id),
            )
        conn.commit()
    finally:
        conn.close()


def query_stats(user_id: int) -> Dict[str, Any]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT aeroapi_period, aeroapi_queries FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    if not row or row["aeroapi_period"] != period:
        return {"period": period, "queries": 0}
    return {"period": row["aeroapi_period"], "queries": row["aeroapi_queries"] or 0}


def budget_state(user_id: int) -> Dict[str, Any]:
    """Estimated spend this month, and whether we've hit the cap.

    ESTIMATED is the operative word: FlightAware's meter isn't visible to
    us, so this is our own tally of result sets multiplied by the published
    rate. The default cap sits under the $5 free credit precisely so an
    estimate being slightly off doesn't produce a bill.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT aeroapi_budget, aeroapi_allow_overage FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    budget = float(row["aeroapi_budget"]) if row and row["aeroapi_budget"] is not None else MONTHLY_BUDGET_USD
    allow_overage = bool(row["aeroapi_allow_overage"]) if row else False

    stats = query_stats(user_id)
    spent = stats["queries"] * COST_PER_QUERY_USD
    over = spent >= budget
    return {
        "queries": stats["queries"],
        "period": stats["period"],
        "spent": round(spent, 2),
        "budget": round(budget, 2),
        "allow_overage": allow_overage,
        "over_budget": over,
        # Only actually stops when the pilot hasn't opted into overage.
        "exhausted": over and not allow_overage,
        "cost_per_query": COST_PER_QUERY_USD,
    }


def credentials(user_id: int) -> Optional[str]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT aeroapi_enabled, aeroapi_key FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["aeroapi_enabled"]:
        return None
    key = (row["aeroapi_key"] or "").strip()
    return key or None


# ------------------------------------------------------------- decisions
def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def is_finished(enr: Optional[Dict[str, Any]]) -> bool:
    """Nothing more to learn about this leg."""
    if not enr:
        return False
    return bool(enr.get("actual_in")) or bool(enr.get("cancelled"))


def _cruise_checkpoints(leg, enr, dep_anchor: datetime) -> list:
    """Evenly spaced mid-flight checks between departure and arrival.

    Anchored to the ESTIMATED wheels-on where the airline has given one,
    so the spacing follows the actual flight rather than the timetable.
    Candidates falling inside MIN_QUERY_GAP of the previous query are
    skipped by the caller, which is why a short leg quietly uses fewer.
    """
    end = None
    for key in ("estimated_on", "scheduled_on", "estimated_in", "scheduled_in"):
        end = _parse((enr or {}).get(key))
        if end:
            break
    if end is None:
        end = leg.arr_datetime_utc()
    # If the only arrival figure we have predates the actual departure, it's
    # stale — a delayed flight's scheduled arrival is already in the past.
    # Project the scheduled block time forward from when it actually left,
    # so cruise checks start straight away instead of waiting for the
    # airline to publish a revised estimate (which needs a query we haven't
    # got a reason to spend yet).
    if end is None or end <= dep_anchor:
        dep_sched, arr_sched = leg.dep_datetime_utc(), leg.arr_datetime_utc()
        if dep_sched and arr_sched and arr_sched > dep_sched:
            end = dep_anchor + (arr_sched - dep_sched)
        else:
            return []
    span = (end - dep_anchor).total_seconds()
    return [dep_anchor + timedelta(seconds=span * (i + 1) / (CRUISE_CHECKS + 1))
            for i in range(CRUISE_CHECKS)]


def should_query(enr: Optional[Dict[str, Any]], leg, now: datetime,
                 queries_used: int, down: bool = False,
                 touchdown: Optional[datetime] = None,
                 has_adsb: bool = False, departed: bool = False,
                 took_off_at: Optional[datetime] = None) -> Optional[str]:
    """Why this leg deserves a query right now, or None.

    Deliberately mostly CLOCK-driven. An earlier version triggered on "a
    position was stored", which sounds like a state change but is true on
    nearly every poll of an airborne aircraft — it burned 8 queries mid-
    cruise telling us nothing. ADS-B is now consulted only where it saves
    a query (touchdown) or where its absence needs covering.
    """
    if queries_used >= MAX_QUERIES_PER_LEG:
        return None
    if is_finished(enr):
        return None

    dep = leg.dep_datetime_utc()
    last = _parse((enr or {}).get("_fetched_at"))

    # The final few queries belong to closeout alone, so a long flight
    # can't leave nothing for confirming gate-in.
    # Past this point only arrival-confirming triggers may spend.
    reserve_only = queries_used >= (MAX_QUERIES_PER_LEG - ARRIVAL_RESERVE)

    # 1. First look — gate assignment and any delay already published.
    if enr is None:
        if dep and now >= dep - PREVIEW_BEFORE_DEP:
            return "T-60: first look"
        return None

    # Closeout is exempt from the ordinary rate floor; everything else
    # respects it.
    closeout_ready = (
        down and not enr.get("actual_in")
        and int(enr.get("_closeout_tries", 0)) < MAX_CLOSEOUT_TRIES
        and (not last or (now - last) >= CLOSEOUT_GAP)
    )
    arr_ref = _parse(enr.get("estimated_in")) or _parse(enr.get("scheduled_in")) or leg.arr_datetime_utc()
    if closeout_ready and arr_ref and now <= arr_ref + CLOSEOUT_WINDOW:
        return "closeout: waiting on gate-in"

    # The no-ADS-B fallback confirms arrival just as closeout does, so it
    # shares the reserve rather than being locked out by it.
    if (not has_adsb and not enr.get("actual_in") and arr_ref
            and int(enr.get("_fallback_tries", 0)) < MAX_ARRIVAL_FALLBACK_TRIES
            and now >= arr_ref + ARRIVAL_FALLBACK
            and (not last or (now - last) >= MIN_QUERY_GAP)):
        return "arrival due (no ADS-B)"

    if reserve_only:
        return None
    if last and (now - last) < MIN_QUERY_GAP:
        return None


    # 3. Wheels down + 5 — often already on stand, which closes the leg in
    #    one query rather than a string of closeout polls.
    # Once only: fire just the first time, when nothing has been asked
    # since the aircraft landed. Without this it re-fires after the
    # closeout attempts run out and quietly doubles the arrival spend.
    if (touchdown and not enr.get("actual_in")
            and now >= touchdown + AFTER_LANDING
            and (not last or last < touchdown)):
        return "wheels down + 5"

    # 2. Still on the ground past departure. One prompt check at T+15 to
    #    see whether it has gone, then a much slower watch while it hasn't
    #    — capped, so a four-hour ground delay can't drain the leg's
    #    budget by asking the same question every fifteen minutes.
    if (not departed and dep and now >= dep + SILENT_DEP_FALLBACK
            and not enr.get("actual_off")):
        tries = int(enr.get("_delay_tries", 0))
        if tries == 0:
            return "T+15: departure check"
        if tries < MAX_DELAY_WATCH_TRIES and (not last or (now - last) >= DELAY_WATCH_GAP):
            return f"still on the ground ({tries} of {MAX_DELAY_WATCH_TRIES - 1})"

    # 5. Cruise checkpoints, evenly spaced across the flight. Only once the
    #    aircraft has ACTUALLY departed — an anchor alone isn't departure.
    # Prefer the airline's wheels-off, but fall back to when ADS-B saw it
    # get airborne. Without that, a departure detected between ground
    # checks would sit idle until the airline caught up — the cruise
    # checks had no anchor to measure from.
    # Anchor on the airline's wheels-off where we have it, otherwise on
    # what ADS-B saw. Without the fallback a flight that departs between
    # ground checks has no anchor at all, so cruise checks couldn't begin
    # until some later query happened to supply actual_off.
    anchor = (_parse(enr.get("actual_off")) or took_off_at
              or _parse(enr.get("actual_out")))
    if departed and anchor and not enr.get("actual_on"):
        # Take the LATEST checkpoint that's due, not the earliest. When the
        # 15-minute floor holds a query back, the checkpoint it was holding
        # is genuinely skipped rather than queued up and fired late — which
        # is what "a short leg may not use them all" means in practice.
        points = _cruise_checkpoints(leg, enr, anchor)
        for i in range(len(points) - 1, -1, -1):
            when = points[i]
            if now >= when and (not last or last < when):
                return f"cruise check {i + 1} of {CRUISE_CHECKS}"

    return None


def refresh(user_id: int, leg, now: datetime, down: bool = False,
            touchdown: Optional[datetime] = None,
            has_adsb: bool = False, departed: bool = False,
            took_off_at: Optional[datetime] = None) -> Optional[str]:
    """Refresh this leg's enrichment if the budget rules allow.

    Returns the reason a query was spent, or None. Never raises — an
    unreachable or rejected API must not disturb tracking, which works
    perfectly well without it.
    """
    api_key = credentials(user_id)
    if not api_key:
        return None

    # Hard stop: never spend past the monthly budget.
    budget = budget_state(user_id)
    if budget["exhausted"]:
        print(f"[enrichment] monthly cap reached "
              f"(~${budget['spent']:.2f} of ${budget['budget']:.2f}) — "
              f"AeroAPI paused, ADS-B tracking continues")
        return None

    enr = get_enrichment(user_id, leg.id)
    used = int((enr or {}).get("_queries", 0))
    departed = departed or bool((enr or {}).get("actual_off"))
    reason = should_query(enr, leg, now, used, down=down, touchdown=touchdown,
                          has_adsb=has_adsb, departed=departed,
                          took_off_at=took_off_at)
    if not reason:
        return None

    try:
        fresh, raw, billed = fetch_leg(api_key, leg.callsign, leg.origin, leg.destination,
                                       leg.dep_datetime_utc(), want_raw=True)
    except AeroApiError as e:
        print(f"[enrichment] {leg.id}: {e}")
        # Still counts against the key's usage — the request was made.
        _count_query(user_id, now, 1)
        return None
    except Exception as e:
        print(f"[enrichment] {leg.id}: unexpected error: {e}")
        return None

    _count_query(user_id, now, billed)
    if fresh is None:
        print(f"[enrichment] {leg.id}: no matching flight for {leg.callsign} "
              f"{leg.origin}-{leg.destination}")
        return reason

    fresh["_queries"] = used + 1
    prev = enr or {}
    fresh["_closeout_tries"] = int(prev.get("_closeout_tries", 0)) + (1 if reason.startswith("closeout") else 0)
    fresh["_fallback_tries"] = int(prev.get("_fallback_tries", 0)) + (1 if reason.startswith("arrival due") else 0)
    fresh["_delay_tries"] = int(prev.get("_delay_tries", 0)) + (1 if (reason.startswith("T+15") or reason.startswith("still on the ground")) else 0)
    _store(user_id, leg.id, fresh, now, raw=raw)
    print(f"[enrichment] {leg.id}: refreshed ({reason})")
    return reason


# ------------------------------------------------------- status & delay
# How far from schedule still counts as "on time" and stays the normal
# colour. Airlines conventionally use 15 minutes; 5 is tighter, because a
# family member watching wants to know sooner than the DOT does.
ON_TIME_TOLERANCE_MIN = 5


# How far along a flight each phase is. Used to combine two sources that
# each go stale in different ways, rather than letting either one win
# outright.
# How far past schedule a departure estimate has to move before the card
# says "Delayed" rather than "Departing".
DELAY_STATUS_MIN = 10

PHASE_ORDER = {
    "Unknown": -1, "Scheduled": 0, "Delayed": 0, "Cancelled": 0,
    "Departing": 1, "Taxi-out": 1,
    "In Air": 2, "Diverting": 2,
    "Landing": 3,
    "Taxi-in": 4,
    "Arrived": 5, "Diverted": 5,
}


def _ooi_phase(enr: Optional[Dict[str, Any]]) -> Optional[str]:
    if not enr:
        return None
    diverted = bool(enr.get("diverted"))
    if enr.get("actual_in"):
        return "Diverted" if diverted else "Arrived"
    if enr.get("actual_on"):
        return "Diverted" if diverted else "Taxi-in"
    if enr.get("actual_off"):
        return "Diverting" if enr.get("diverted") else "In Air"
    if enr.get("actual_out"):
        return "Taxi-out"
    return None


def derive_status(enr: Optional[Dict[str, Any]], adsb_status: Optional[str]) -> Optional[str]:
    """Flight phase from OOOI and ADS-B together — whichever is further along.

    Neither source can be trusted alone, and they fail in OPPOSITE
    directions:

      * OOOI runs LATE. actual_on and actual_in are published with a lag,
        so a flight that has visibly landed still reports "In Air".
      * ADS-B runs BLIND. No receiver near a small field means no ground
        state at all, so a landed aircraft just disappears.

    An earlier version returned the first matching OOOI field and never
    consulted ADS-B afterwards, which meant a flight sat at "In Air" while
    ADS-B plainly showed it stopped on the ground at the destination.

    So: rank both and take the more advanced. Whichever notices first
    wins, and neither can drag the flight backwards.
    """
    if enr and enr.get("cancelled"):
        return "Cancelled"

    # "Departing" from the ADS-B side only means the scheduled departure
    # time has passed with nothing seen yet — it is NOT evidence the
    # aircraft is going anywhere. If the airline has pushed the estimate
    # and there's still no gate-out, the flight is delayed, and saying
    # "Departing" for a flight sitting at the gate is actively misleading.
    if enr and not enr.get("actual_out") and not enr.get("actual_off"):
        pushed = _parse(enr.get("estimated_out"))
        sched = _parse(enr.get("scheduled_out"))
        if pushed and sched and (pushed - sched).total_seconds() >= DELAY_STATUS_MIN * 60:
            if adsb_status in (None, "Scheduled", "Departing", "Taxi-out"):
                return "Delayed"

    ooi = _ooi_phase(enr)
    if ooi is None:
        return adsb_status or "Scheduled"
    if not adsb_status:
        return ooi

    ooi_rank = PHASE_ORDER.get(ooi, -1)
    adsb_rank = PHASE_ORDER.get(adsb_status, -1)
    if adsb_rank > ooi_rank:
        return adsb_status
    # Keep the diversion wording when OOOI knows about it and ADS-B can't.
    if ooi == "Diverting" and adsb_rank <= ooi_rank:
        return "Diverting"
    return ooi


def _fmt_local(dt: Optional[datetime], tz_name: Optional[str],
               time_format: str = "24", with_zone: bool = True) -> Optional[str]:
    """A UTC instant as a clock time at the airport, with its zone."""
    if dt is None or not tz_name:
        return None
    try:
        local = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        return None
    if time_format == "12":
        text = local.strftime("%I:%M %p").lstrip("0")
    else:
        text = local.strftime("%H:%M")
    if not with_zone:
        return text
    abbr = local.tzname() or tz_name.split("/")[-1]
    return f"{text} {abbr}"


def _span(minutes: int) -> str:
    amount = abs(minutes)
    if amount >= 60:
        return f"{amount // 60}h {amount % 60:02d}m"
    return f"{amount} min"


def _delay_for(enr, baseline, actual_key, estimate_key, tz_name, time_format,
               verb_future, verb_past, tolerance, observed=None, settled_override=None):
    """Shared shape for the departure and arrival delay blocks.

    `baseline` is the FFDO SCHEDULED time — the pilot's own bid line, not
    the airline's published schedule. Those can differ by a couple of
    minutes, and the pilot flies to the FFDO, so that's what "late" is
    measured against here.
    """
    if not enr or baseline is None:
        return None
    actual = _parse(enr.get(actual_key))
    estimate = _parse(enr.get(estimate_key))
    # Order matters: the airline's actual, then what WE observed, then the
    # forecast. An observed gate stop is a fact; estimated_in is not, and
    # presenting a forecast as the arrival time is misleading once the
    # aircraft is demonstrably parked.
    compare = actual or observed or estimate
    if compare is None:
        return None

    # Truncate BOTH to the minute before doing anything else. The displayed
    # clock time drops seconds, so if the delta were computed from the raw
    # instants it could round the other way and leave the note disagreeing
    # with the time printed next to it by a minute — exactly the mismatch
    # this whole block exists to fix.
    compare = compare.replace(second=0, microsecond=0)
    baseline = baseline.replace(second=0, microsecond=0)

    minutes = int(round((compare - baseline).total_seconds() / 60))
    # Past tense follows the FLIGHT, not just whether the airline has
    # published a figure — a card reading "Arrived" beside "Arrives 11 min
    # early" is self-contradictory.
    settled = (actual is not None) or (observed is not None)
    if settled_override is not None:
        settled = settled_override
    revised = _fmt_local(compare, tz_name, time_format)
    original = _fmt_local(baseline, tz_name, time_format)
    revised_short = _fmt_local(compare, tz_name, time_format, with_zone=False)
    original_short = _fmt_local(baseline, tz_name, time_format, with_zone=False)

    if abs(minutes) <= tolerance:
        return {
            "state": "ontime", "minutes": minutes, "settled": settled,
            "time": revised, "original": original,
            "time_short": revised_short, "original_short": original_short,
            "text": f"{verb_past} on time" if settled else "On time",
        }

    word = "late" if minutes > 0 else "early"
    verb = verb_past if settled else verb_future
    return {
        "state": "late" if minutes > 0 else "early",
        "minutes": minutes,
        "settled": settled,
        "time": revised,
        # Only worth showing the original when it actually differs.
        "original": original,
        "time_short": revised_short,
        "original_short": original_short,
        "text": f"{verb} {_span(minutes)} {word}",
        "short_text": f"{_span(minutes)} {word}",
    }


def departure_delay(enr, leg, time_format: str = "24", tolerance: int = None,
                    status: Optional[str] = None):
    """Is he getting out? Measured against the FFDO departure time.

    Separate from arrival on purpose: a maintenance delay at the gate is
    news long before it shows up as a late arrival, and sometimes a late
    departure makes the time up enroute and never becomes one at all.
    """
    tol = ON_TIME_TOLERANCE_MIN if tolerance is None else tolerance
    tz = getattr(leg.origin_info, "timezone", None) if leg.origin_info else None
    departed = status in ("In Air", "Landing", "Taxi-in", "Arrived", "Diverting") if status else None
    return _delay_for(enr, leg.dep_datetime_utc(), "actual_out", "estimated_out",
                      tz, time_format, "Departing", "Departed", tol,
                      settled_override=departed if departed else None)


def arrival_delay(enr, leg, time_format: str = "24", tolerance: int = None,
                  status: Optional[str] = None, observed_in=None):
    """When does he get there? Measured against the FFDO arrival time."""
    tol = ON_TIME_TOLERANCE_MIN if tolerance is None else tolerance
    tz = getattr(leg.dest_info, "timezone", None) if leg.dest_info else None
    if enr and enr.get("cancelled"):
        return {"state": "cancelled", "minutes": 0, "settled": True,
                "time": None, "original": None, "text": "Cancelled"}
    arrived = (status == "Arrived") if status else None
    return _delay_for(enr, leg.arr_datetime_utc(), "actual_in", "estimated_in",
                      tz, time_format, "Arrives", "Arrived", tol,
                      observed=observed_in,
                      settled_override=True if arrived else None)


def gate_info(enr: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not enr:
        return None
    out = {
        "origin_gate": enr.get("gate_origin"),
        "origin_terminal": enr.get("terminal_origin"),
        "dest_gate": enr.get("gate_destination"),
        "dest_terminal": enr.get("terminal_destination"),
        "baggage": enr.get("baggage_claim"),
    }
    return out if any(out.values()) else None


def diversion_info(enr: Optional[Dict[str, Any]], scheduled_dest: str) -> Optional[Dict[str, Any]]:
    """Where it's actually going, if that stopped being the plan."""
    if not enr or not enr.get("diverted"):
        return None
    actual_dest = enr.get("destination")
    if actual_dest and actual_dest != (scheduled_dest or "").upper():
        return {"diverted_to": actual_dest, "scheduled": scheduled_dest}
    return {"diverted_to": None, "scheduled": scheduled_dest}
