"""When to spend an AeroAPI query, and where the answer goes.

The pilot's own key pays for this, so the budget rules matter as much as
the data:

  * ONLY the background poller queries. Page renders read the row, so a
    family member hitting refresh fifty times during a delay costs nothing.
  * Triggers are almost all CLOCK-driven. An earlier version triggered on
    "a position was stored", which sounds like a state change but is true
    on nearly every poll of an airborne aircraft — it burned 8 queries
    mid-cruise telling us nothing and hit the per-leg cap on an ordinary
    flight. ADS-B is consulted only where it SAVES a query (touchdown) or
    where its absence needs covering.
  * Every trigger has its own cap; none borrows from another.

The answer is written into named columns on the flight row, not a JSON
blob. Airline values land in the `_api` / `_estimated` / `_scheduled`
columns; nothing here ever touches an `_observed` column, so a lagging
airline record can never overwrite something we watched happen.

WHAT GETS QUERIED, AND WHEN — THE TICKET RULE
---------------------------------------------
Every leg is handed TICKETS_PER_LEG tickets and one rule for spending
them:

    time left in the window / tickets left = how long to wait

That's it. No trigger list, no per-trigger caps, no special case for a
delay. The window runs from 30 minutes before scheduled departure to an
hour after the BEST CURRENTLY KNOWN arrival — so when the airline
publishes a revised arrival, the window stretches by itself and the
remaining tickets re-space themselves across it. A six-hour delay widens
the gaps instead of draining the budget, which is exactly what the old
"still on the ground" watcher existed to prevent, without the watcher.

Two clamps keep it honest at the edges:

  MIN_QUERY_GAP   never faster than this, so a garbage timestamp can't
                  empty the wallet in one sweep
  MAX_QUERY_GAP   never slower than this. Also what covers the case
                  where the flight overruns its window and the airline
                  has published nothing: remaining time goes negative,
                  the formula falls through to this, and the leg ticks
                  over quietly instead of stopping dead.

And ARRIVAL_RESERVE of the tickets are locked until the aircraft is
actually down or past its arrival time. Gate-in is the one answer that
ends the leg, so it cannot be starved by a long delay upstream.

The leg stops spending the moment there is nothing left to learn — gate-in
received, cancelled, or closed — and unspent tickets simply go unspent.

REPLACED IN v5.2
----------------
Six independent triggers (first look / ground watch / cruise checks /
wheels-down / closeout / no-ADS-B fallback), each with its own cap and its
own counter column. They worked, but the interactions between them were
where the bugs lived, and three cruise checks on a 95-minute regional leg
bought the same answer three times. One rule is easier to reason about and
spends the allowance where it's actually worth something.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .aeroapi import AeroApiError, fetch_leg
from .db import get_connection
from .flights import get_flight, owners_of, write

# How many queries one leg may ever spend. At $0.005 each this is $0.09
# per leg; across a heavy 50-leg month that is $4.50 against the Personal
# tier's $5 free allowance, and real spend lands well under because most
# legs stop early the moment gate-in arrives.
#
# 18 puts a query roughly every 11 minutes across an average 95-minute
# leg's window. Going higher mostly buys the same answer twice — the
# airline does not republish gates and estimates faster than that.
TICKETS_PER_LEG = 18
# Of those, how many are locked until the aircraft is down or past its
# arrival time. Gate-in is the answer that ENDS the leg, so a long delay
# upstream must not be able to spend the tickets that confirm it.
ARRIVAL_RESERVE = 4

# The clamps on the spacing formula. See the module docstring.
MIN_QUERY_GAP = timedelta(minutes=5)
MAX_QUERY_GAP = timedelta(minutes=20)

# The window tickets are spread across: this far before SCHEDULED
# departure, to this far after the best currently known arrival. T-30
# rather than T-60 because by then the flight plan is filed, so the gate,
# the revised arrival and any published delay all actually exist.
WINDOW_BEFORE_DEP = timedelta(minutes=30)
WINDOW_AFTER_ARR = timedelta(minutes=60)

# What a /flights/{ident} call costs, per FlightAware's published rate.
COST_PER_QUERY_USD = float(os.environ.get("AEROAPI_COST_PER_QUERY", "0.005"))
# Fallback ceiling for a user row with no value of its own.
MONTHLY_BUDGET_USD = float(os.environ.get("AEROAPI_MONTHLY_BUDGET", "4.90"))
# /account/usage is free, and FlightAware's figure updates every 10
# minutes, so 15 keeps the brake reading a number that is actually close
# to true. It used to be hourly, which meant the cap could be enforced
# against a reading an hour out of date — the one number that must not be
# stale is the one deciding whether to stop spending.
USAGE_REFRESH = timedelta(minutes=15)
# A reading older than this is treated as a FLOOR rather than the truth.
# Refreshing every 15 minutes, anything over an hour old means the usage
# endpoint is unreachable, not that spending stopped.
USAGE_STALE_AFTER = timedelta(hours=1)


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


# ------------------------------------------------------------ the wallet
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
    return (row["aeroapi_key"] or "").strip() or None


def _count_query(user_id: int, now: datetime, billed: int = 1) -> None:
    """Add this call's RESULT SETS to the month's tally.

    Billing is per result set of up to 15 records, so one call can cost
    more than one. Counting calls would under-report.
    """
    period = now.strftime("%Y-%m")
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT aeroapi_period, aeroapi_queries FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row or row["aeroapi_period"] != period:
            conn.execute("UPDATE users SET aeroapi_period = ?, aeroapi_queries = ? "
                         "WHERE id = ?", (period, billed, user_id))
        else:
            conn.execute("UPDATE users SET aeroapi_queries = aeroapi_queries + ? "
                         "WHERE id = ?", (billed, user_id))
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


def refresh_usage(user_id: int, now: datetime) -> bool:
    """Pull FlightAware's own spend figure. Free, so not budget-limited."""
    api_key = credentials(user_id)
    if not api_key:
        return False
    conn = get_connection()
    try:
        row = conn.execute("SELECT aeroapi_usage_at FROM users WHERE id = ?",
                           (user_id,)).fetchone()
    finally:
        conn.close()
    last = _parse(row["aeroapi_usage_at"]) if row else None
    if last and (now - last) < USAGE_REFRESH:
        return False

    from .aeroapi import fetch_usage
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    usage = fetch_usage(api_key, start=period_start.strftime("%Y-%m-%d"),
                        end=now.strftime("%Y-%m-%d"))
    if not usage:
        return False
    if usage.get("cost") is None:
        # Shape wasn't what we expected. Log it rather than silently
        # reporting zero spend, which would disable the one control that
        # prevents a bill.
        print(f"[enrichment] /account/usage returned an unrecognised shape: "
              f"{str(usage.get('raw'))[:300]}")
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET aeroapi_reported_cost = ?, aeroapi_reported_calls = ?, "
            "aeroapi_usage_at = ? WHERE id = ?",
            (usage.get("cost"), usage.get("calls"), now.isoformat(), user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return True


def _ago(when: Optional[datetime]) -> Optional[str]:
    if not when:
        return None
    mins = (datetime.now(timezone.utc) - when).total_seconds() / 60
    if mins < 1.5:
        return "just now"
    if mins < 90:
        return f"{int(mins)} min ago"
    return f"{int(mins // 60)}h ago"


def budget_state(user_id: int) -> Dict[str, Any]:
    """Spend this month per FlightAware, and whether the cap is reached.

    The DISPLAYED figure is FlightAware's own and nothing else. A local
    estimate used to sit beside it, but two numbers for one thing invites
    the question of which to believe, and the estimate was the wrong one —
    it prices every query at the /flights rate and undercounts any leg
    that needed /schedules. Until a reading arrives the page says so
    rather than showing a number nobody should act on.

    ENFORCEMENT is separate and deliberately more paranoid: a fresh
    reading is used as-is, a stale or missing one falls back to the HIGHER
    of the last reading and the local count. A stale figure is a floor,
    not a ceiling — querying has continued since. That fallback never
    reaches the screen, and it exists so an unreachable usage endpoint
    can't quietly disable the cap.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT aeroapi_budget, aeroapi_reported_cost, aeroapi_reported_calls, "
            "aeroapi_usage_at FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    budget = (float(row["aeroapi_budget"]) if row and row["aeroapi_budget"] is not None
              else MONTHLY_BUDGET_USD)
    stats = query_stats(user_id)
    estimated = round(stats["queries"] * COST_PER_QUERY_USD, 2)

    reported = row["aeroapi_reported_cost"] if row else None
    reported_at = _parse(row["aeroapi_usage_at"]) if row else None
    spent, fresh = None, False
    if reported is not None and reported_at is not None:
        spent = round(float(reported), 2)
        fresh = (datetime.now(timezone.utc) - reported_at) < USAGE_STALE_AFTER

    enforce = spent if fresh else max(spent or 0.0, estimated)
    over = enforce >= budget
    return {
        "queries": stats["queries"],
        "reported_calls": (row["aeroapi_reported_calls"] if row else None),
        "period": stats["period"],
        "has_reading": spent is not None,
        "reading_fresh": fresh,
        "spent": spent,
        "enforced_spend": round(enforce, 2),
        "usage_at": reported_at.isoformat() if reported_at else None,
        "usage_age": _ago(reported_at),
        "budget": round(budget, 2),
        "over_budget": over,
        # The cap is the pilot's own number and is ALWAYS enforced. The
        # old allow-overage toggle meant the one setting that exists to
        # prevent a surprise bill could itself be switched off, which is
        # exactly backwards. Anyone on a paid tier raises the number.
        "exhausted": over,
        "cost_per_query": COST_PER_QUERY_USD,
    }


# --------------------------------------------------------- when to spend
def window_end(row, leg) -> Optional[datetime]:
    """When this leg's query window closes: best known arrival + an hour.

    "Best known" in order of confidence — the airline's live estimate
    first, then its published schedule, then the FFDO timetable. This is
    the whole delay story: the airline publishes a revised arrival, this
    moves, and the spacing formula spreads the remaining tickets across the
    longer window without anything else in the app having to notice.
    """
    arr = None
    for key in ("in_estimated", "on_estimated", "in_scheduled"):
        arr = _parse(_col(row, key))
        if arr:
            break
    if arr is None:
        arr = leg.arr_datetime_utc()
    return (arr + WINDOW_AFTER_ARR) if arr else None


def next_gap(row, leg, now: datetime, tickets_left: int) -> timedelta:
    """How long to wait before the next query. The whole rule.

    Time left in the window divided by tickets left, clamped at both ends.
    Past the end of the window the division goes negative and it falls
    through to MAX_QUERY_GAP — which is the right answer for a flight
    overrunning with nothing published: keep asking, slowly.
    """
    end = window_end(row, leg)
    if end is None or tickets_left <= 0:
        return MAX_QUERY_GAP
    remaining = end - now
    if remaining <= timedelta(0):
        return MAX_QUERY_GAP
    gap = remaining / tickets_left
    return max(MIN_QUERY_GAP, min(MAX_QUERY_GAP, gap))


def should_query(row, leg, now: datetime, has_adsb: bool = False,
                 touchdown: Optional[datetime] = None,
                 departed: bool = False, down: bool = False) -> Optional[str]:
    """Why this leg deserves a query right now, or None.

    The extra arguments are what the poller already knows from ADS-B. Only
    `down` still matters — it unlocks the arrival reserve early, so a
    flight that lands ahead of its estimate gets its gate-in tickets
    straight away rather than waiting for a clock to catch up. The rest are
    kept in the signature because the poller passes them and they cost
    nothing to ignore.
    """
    if row is None:
        return None

    # Nothing left to learn. Gate-in is the answer that ends the leg.
    if _col(row, "in_actual_api") or _col(row, "cancelled") or _col(row, "closed"):
        return None

    used = int(_col(row, "api_queries_used") or 0)
    tickets_left = TICKETS_PER_LEG - used
    if tickets_left <= 0:
        return None

    dep = leg.dep_datetime_utc()
    if dep is None or now < dep - WINDOW_BEFORE_DEP:
        return None   # window hasn't opened

    # The reserve is locked until the aircraft is genuinely down, or its
    # arrival time has come and gone. `down` covers the normal case; the
    # clock covers a leg with no ADS-B coverage at all, where no
    # touchdown can ever be observed.
    arr_ref = (_parse(_col(row, "in_estimated")) or _parse(_col(row, "in_scheduled"))
               or leg.arr_datetime_utc())
    at_arrival = bool(down) or bool(arr_ref and now >= arr_ref)
    if not at_arrival and tickets_left <= ARRIVAL_RESERVE:
        return None

    last = _parse(_col(row, "last_api_query_at"))
    if last is None:
        return f"first look (1 of {TICKETS_PER_LEG})"

    gap = next_gap(row, leg, now, tickets_left)
    if (now - last) < gap:
        return None

    spent = used + 1
    phase = "arrival" if at_arrival else "en route"
    return f"{phase} check ({spent} of {TICKETS_PER_LEG}, {int(gap.total_seconds() // 60)}m spacing)"


# ------------------------------------------------- the late gate-in chase
# How long after CLOSING to ask again for an airline gate-in that never
# arrived, and how many times. Attempt 1 goes at +90 minutes, 2 at +6h,
# 3 at +18h, each measured from the previous attempt.
#
# WHY THESE NUMBERS. The owner's report: usually the airline's gate-in is
# quick, but a leg that blocked in at 07:00 still had nothing by 11:30.
# So "quick" is already covered by the leg's own live tickets and needs
# nothing here; what needs covering is the several-hour tail and the
# occasional overnight. Three attempts reach ~24 hours past block-in,
# which covers both without the schedule ever becoming open-ended.
#
# WHY IT IS BOUNDED AT ALL. `carrier.py` learned this the expensive way: a
# lookup that records nothing on failure gets asked again on the next
# sweep, forever. Every attempt here is written to the row BEFORE the call
# goes out, so a timeout or a crash mid-request still counts.
GATEIN_BACKFILL_GAPS = [timedelta(minutes=90), timedelta(hours=6),
                        timedelta(hours=18)]
GATEIN_BACKFILL_TRIES = len(GATEIN_BACKFILL_GAPS)


def should_backfill_gate_in(row, now: datetime) -> Optional[str]:
    """Is it time to go back and ask for this closed leg's gate-in?

    Deliberately NOT part of `should_query`. That function refuses to spend
    anything on a closed leg, which is right for the live ticket allowance
    — there is nothing left to watch. But it also made
    `closure.maybe_close`'s upgrade path unreachable: the one value that
    could upgrade a provisional close was the one value nothing would ever
    fetch again. Two different questions, so two functions.
    """
    if row is None:
        return None
    if not _col(row, "closed") or _col(row, "cancelled"):
        return None
    if _col(row, "in_actual_api"):
        return None                    # already have it; nothing to chase
    if _col(row, "closed_by") == "airline":
        return None                    # closed ON gate-in; cannot be missing

    tries = int(_col(row, "gatein_tries") or 0)
    if tries >= GATEIN_BACKFILL_TRIES:
        return None

    since = _parse(_col(row, "gatein_tried_at")) or _parse(_col(row, "closed_at"))
    if since is None:
        return None
    if now - since < GATEIN_BACKFILL_GAPS[tries]:
        return None
    return f"late gate-in check ({tries + 1} of {GATEIN_BACKFILL_TRIES})"


def backfill_gate_in(user_id: int, leg, now: datetime) -> Optional[str]:
    """One late attempt at the airline's gate-in for an already-closed leg.

    Returns the reason a query was spent, or None. Obeys the same monthly
    cap as everything else — this is real money, just a very small amount
    of it: at most three queries on only those legs that finished without
    an airline gate-in.
    """
    row = get_flight(leg.id)
    if row is not None and _col(row, "simulated"):
        return None                    # see the guard in refresh()

    api_key = credentials(user_id)
    if not api_key:
        return None
    if budget_state(user_id)["exhausted"]:
        return None

    reason = should_backfill_gate_in(row, now)
    if not reason:
        return None

    # Recorded BEFORE the call. See the comment on GATEIN_BACKFILL_GAPS.
    write(leg.id, always={
        "gatein_tries": int(_col(row, "gatein_tries") or 0) + 1,
        "gatein_tried_at": now.isoformat(),
    })

    try:
        data, raw, billed = fetch_leg(api_key, leg.callsign, leg.origin,
                                      leg.destination, leg.dep_datetime_utc(),
                                      want_raw=True)
    except AeroApiError as e:
        print(f"[enrichment] {leg.id}: late gate-in check failed: {e}")
        _count_query(user_id, now, 1)
        return None
    except Exception as e:
        print(f"[enrichment] {leg.id}: late gate-in check errored: {e}")
        return None

    _count_query(user_id, now, billed)
    if data is None:
        return reason
    _apply(user_id, leg.id, data, raw, now, reason, get_flight(leg.id))
    print(f"[enrichment] {leg.id}: {reason}")
    return reason


# --------------------------------------------------------------- spending
def _apply(user_id: int, leg_id: str, data: Dict[str, Any], raw, now: datetime,
           reason: str, row) -> None:
    """Write one AeroAPI record into the flight row.

    Actuals are ONCE — they describe a moment that already happened, and
    letting a later restatement overwrite them is how a good value gets
    replaced by a worse one. Estimates and gates are LATEST, because those
    genuinely move. Nothing here writes an `_observed` column.
    """
    first_time = _col(row, "out_scheduled") is None
    once = {
        "fa_flight_id": data.get("fa_flight_id"),
        "out_actual_api": data.get("actual_out"),
        "off_actual_api": data.get("actual_off"),
        "on_actual_api": data.get("actual_on"),
        "in_actual_api": data.get("actual_in"),
    }
    if first_time:
        # Snapshot the airline's ORIGINAL published times. Airlines amend
        # schedules; without this, "was 11:55" becomes unanswerable.
        once.update({
            "out_scheduled": data.get("scheduled_out"),
            "off_scheduled": data.get("scheduled_off"),
            "on_scheduled": data.get("scheduled_on"),
            "in_scheduled": data.get("scheduled_in"),
        })
    latest = {
        "out_estimated": data.get("estimated_out"),
        "off_estimated": data.get("estimated_off"),
        "on_estimated": data.get("estimated_on"),
        "in_estimated": data.get("estimated_in"),
        "gate_origin": data.get("gate_origin"),
        "gate_destination": data.get("gate_destination"),
        "terminal_origin": data.get("terminal_origin"),
        "terminal_destination": data.get("terminal_destination"),
        "baggage_claim": data.get("baggage_claim"),
        "status_text": data.get("status_text"),
        "tail_api": data.get("registration"),
        "destination_actual": data.get("destination"),
    }
    always = {
        "last_api_query_at": now.isoformat(),
        "last_api_reason": reason,
        # Whose key paid. The row is shared, so this is the only record of
        # which account the spend belongs to.
        "api_paid_by": user_id,
        # The one counter that matters now: tickets spent. The old
        # closeout_tries / fallback_tries / delay_watch_tries columns are
        # left on the table (migrations here are append-only) but nothing
        # reads or writes them any more — each existed to cap one of the
        # six triggers the ticket rule replaced.
        "api_queries_used": int(_col(row, "api_queries_used") or 0) + 1,
    }
    # Cancelled and diverted are one-way. A later record that omits them
    # must not un-cancel a cancelled flight.
    if data.get("cancelled"):
        always["cancelled"] = 1
    if data.get("diverted"):
        always["diverted"] = 1
    if data.get("blocked"):
        always["blocked"] = 1
    # Every query costs money, so the full untouched record is kept —
    # throwing away fields we don't currently render would mean paying
    # again later for data we already had.
    if raw is not None:
        always["api_raw"] = json.dumps(raw)[:200000]

    write(leg_id, once=once, latest=latest, always=always)


def refresh(user_id: int, leg, now: datetime, has_adsb: bool = False,
            touchdown: Optional[datetime] = None, departed: bool = False,
            down: bool = False) -> Optional[str]:
    """Refresh this leg's airline data if the budget rules allow.

    Returns the reason a query was spent, or None. Never raises — an
    unreachable or rejected API must not disturb tracking, which works
    perfectly well without it.
    """
    # THE TEST-MODE GUARD. First, before the key is even read, and
    # repeated in backfill_gate_in below. A simulated leg is an invention;
    # asking FlightAware about it would spend real money on a flight that
    # does not exist, and could return a REAL flight that happens to share
    # the callsign, quietly mixing invented and genuine data in one row.
    # This is the single most important line in test mode.
    row = get_flight(leg.id)
    if row is not None and _col(row, "simulated"):
        return None

    api_key = credentials(user_id)
    if not api_key:
        return None

    budget = budget_state(user_id)
    if budget["exhausted"]:
        print(f"[enrichment] monthly cap reached "
              f"(${budget['enforced_spend']:.2f} of ${budget['budget']:.2f}, "
              f"{'per FlightAware' if budget['reading_fresh'] else 'local count'}) — "
              f"AeroAPI paused, ADS-B tracking continues")
        return None

    row = get_flight(leg.id)
    if row is None:
        return None
    departed = departed or bool(_col(row, "off_actual_api"))
    reason = should_query(row, leg, now, has_adsb=has_adsb, touchdown=touchdown,
                          departed=departed, down=down)
    if not reason:
        return None

    try:
        data, raw, billed = fetch_leg(api_key, leg.callsign, leg.origin,
                                      leg.destination, leg.dep_datetime_utc(),
                                      want_raw=True)
    except AeroApiError as e:
        print(f"[enrichment] {leg.id}: {e}")
        # Still counts against the key's usage — the request was made.
        _count_query(user_id, now, 1)
        # And against the leg, so a failing key can't loop forever.
        write(leg.id, always={
            "last_api_query_at": now.isoformat(),
            "api_paid_by": user_id,
            "api_queries_used": int(_col(row, "api_queries_used") or 0) + 1,
        })
        return None
    except Exception as e:
        print(f"[enrichment] {leg.id}: unexpected error: {e}")
        return None

    _count_query(user_id, now, billed)
    if data is None:
        print(f"[enrichment] {leg.id}: no matching flight for {leg.callsign} "
              f"{leg.origin}-{leg.destination}")
        write(leg.id, always={
            "last_api_query_at": now.isoformat(),
            "last_api_reason": reason,
            "api_paid_by": user_id,
            "api_queries_used": int(_col(row, "api_queries_used") or 0) + 1,
        })
        return reason

    _apply(user_id, leg.id, data, raw, now, reason, row)
    print(f"[enrichment] {leg.id}: refreshed ({reason})")
    return reason


def payer_for(leg_id: str) -> Optional[int]:
    """Which crew member's AeroAPI key should pay for this flight.

    The row is shared, so only ONE query is needed however many crew are on
    the leg — the point of sharing. Picks the lowest user id that has a key
    enabled AND budget left; if that pilot is capped for the month, the
    next one covers it, and the flight keeps its airline data instead of
    going dark for everyone.
    """
    for uid in owners_of(leg_id):
        if credentials(uid) and not budget_state(uid)["exhausted"]:
            return uid
    return None
