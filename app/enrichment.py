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

WHAT GETS QUERIED, AND WHEN
---------------------------
  T-30            first look: gate, revised arrival, any published delay.
                  By then the flight plan is filed so all three exist; an
                  hour out they usually don't yet.
  T+20, then 30m  while still ON THE GROUND past departure. Capped at 3.
                  Stops the moment ADS-B sees it airborne.
  3 cruise checks evenly spaced between ACTUAL departure and estimated
                  wheels-on. Requires real evidence of being airborne —
                  gated only on an anchor, they fired while a delayed
                  flight sat at the gate.
  Wheels down +5  often already on stand, closing the leg in one query
                  instead of several. Fires once. Needs ADS-B.
  Closeout        every 10 min until gate-in. Capped at 2.
  Arrival fallback for legs with NO ADS-B at all, where no wheels-down
                  event can ever fire. Capped at 2.

Reachable maximum is 10 with ADS-B and 9 without, against a hard ceiling
of MAX_QUERIES_PER_LEG, of which ARRIVAL_RESERVE are held for confirming
arrival alone. Typical is 5 per leg, worst case 8.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .aeroapi import AeroApiError, fetch_leg
from .db import get_connection
from .flights import get_row, write

# Never query a leg more often than this, whatever the triggers say.
MIN_QUERY_GAP = timedelta(minutes=20)
# How many evenly spaced checks between departure and arrival. Any falling
# inside MIN_QUERY_GAP of the previous query are skipped outright (the
# latest due checkpoint wins), so a short leg simply uses fewer.
CRUISE_CHECKS = 3
# Absolute ceiling per leg — a runaway loop can't cost more than this.
MAX_QUERIES_PER_LEG = 10
# Of that ceiling, how many are held back purely for confirming arrival —
# closeout, or the no-ADS-B fallback. Both answer the same question ("has
# it blocked in?"), and that question is the one that closes the leg, so
# it can't be starved by earlier triggers.
ARRIVAL_RESERVE = 2
# Hard caps on the repeating triggers. Both loop on a timer until they get
# an answer, and when gate-in simply never publishes — which happens — an
# uncapped loop eats the whole per-leg budget waiting for something that
# isn't coming. After these, the backstop closes the leg instead.
MAX_CLOSEOUT_TRIES = 2
MAX_ARRIVAL_FALLBACK_TRIES = 2
MAX_DELAY_WATCH_TRIES = 3
DELAY_WATCH_GAP = timedelta(minutes=30)
CLOSEOUT_GAP = timedelta(minutes=10)
CLOSEOUT_WINDOW = timedelta(minutes=90)

# How early to take a first look. T-30 rather than T-60: by then the
# flight plan is filed, so the revised arrival, any published delay and
# the gate all exist.
PREVIEW_BEFORE_DEP = timedelta(minutes=30)
SILENT_DEP_FALLBACK = timedelta(minutes=20)
AFTER_LANDING = timedelta(minutes=5)
ARRIVAL_FALLBACK = timedelta(minutes=10)

# What a /flights/{ident} call costs, per FlightAware's published rate.
COST_PER_QUERY_USD = float(os.environ.get("AEROAPI_COST_PER_QUERY", "0.005"))
# Fallback ceiling for a user row with no value of its own.
MONTHLY_BUDGET_USD = float(os.environ.get("AEROAPI_MONTHLY_BUDGET", "4.50"))
# /account/usage is free, and their figure only updates every 10 minutes,
# so there is no point asking more often than this.
USAGE_REFRESH = timedelta(hours=1)
# A reading older than this is treated as a FLOOR rather than the truth.
USAGE_STALE_AFTER = timedelta(hours=3)


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
def _cruise_checkpoints(leg, row, dep_anchor: datetime) -> List[datetime]:
    """Evenly spaced mid-flight checks between departure and arrival."""
    end = None
    for key in ("on_estimated", "on_scheduled", "in_estimated", "in_scheduled"):
        end = _parse(_col(row, key))
        if end:
            break
    if end is None:
        end = leg.arr_datetime_utc()
    # If the only arrival figure predates the actual departure it's stale —
    # a delayed flight's scheduled arrival is already in the past. Project
    # the scheduled block time forward from when it actually left, so
    # cruise checks start straight away instead of waiting for the airline
    # to publish a revised estimate we haven't got a reason to buy yet.
    if end is None or end <= dep_anchor:
        dep_sched, arr_sched = leg.dep_datetime_utc(), leg.arr_datetime_utc()
        if dep_sched and arr_sched and arr_sched > dep_sched:
            end = dep_anchor + (arr_sched - dep_sched)
        else:
            return []
    span = (end - dep_anchor).total_seconds()
    return [dep_anchor + timedelta(seconds=span * (i + 1) / (CRUISE_CHECKS + 1))
            for i in range(CRUISE_CHECKS)]


def should_query(row, leg, now: datetime, has_adsb: bool = False,
                 touchdown: Optional[datetime] = None,
                 departed: bool = False, down: bool = False) -> Optional[str]:
    """Why this leg deserves a query right now, or None."""
    if row is None:
        return None
    used = int(_col(row, "api_queries_used") or 0)
    if used >= MAX_QUERIES_PER_LEG:
        return None
    # Nothing more to learn.
    if _col(row, "in_actual_api") or _col(row, "cancelled") or _col(row, "closed"):
        return None

    dep = leg.dep_datetime_utc()
    last = _parse(_col(row, "last_api_query_at"))

    # 1. First look. Nothing has been asked yet.
    if last is None:
        if dep and now >= dep - PREVIEW_BEFORE_DEP:
            return "T-30: first look"
        return None

    # Past this point only arrival-confirming triggers may spend.
    reserve_only = used >= (MAX_QUERIES_PER_LEG - ARRIVAL_RESERVE)

    arr_ref = (_parse(_col(row, "in_estimated")) or _parse(_col(row, "in_scheduled"))
               or leg.arr_datetime_utc())

    # 2. Closeout — exempt from the ordinary rate floor, because the
    #    question it asks is the one that ends the leg.
    if (down and int(_col(row, "closeout_tries") or 0) < MAX_CLOSEOUT_TRIES
            and (now - last) >= CLOSEOUT_GAP
            and arr_ref and now <= arr_ref + CLOSEOUT_WINDOW):
        return "closeout: waiting on gate-in"

    # 3. No-ADS-B arrival fallback. Shares the reserve with closeout
    #    rather than being locked out by it — for a leg with no coverage
    #    at all, no wheels-down event can ever fire.
    if (not has_adsb and arr_ref
            and int(_col(row, "fallback_tries") or 0) < MAX_ARRIVAL_FALLBACK_TRIES
            and now >= arr_ref + ARRIVAL_FALLBACK
            and (now - last) >= MIN_QUERY_GAP):
        return "arrival due (no ADS-B)"

    if reserve_only:
        return None
    if (now - last) < MIN_QUERY_GAP:
        return None

    # 4. Wheels down + 5. Fires ONCE — without the `last < touchdown`
    #    guard it re-fires after the closeout attempts run out and quietly
    #    doubles the arrival spend.
    if touchdown and now >= touchdown + AFTER_LANDING and last < touchdown:
        return "wheels down + 5"

    # 5. Still on the ground past departure. One prompt check, then a
    #    slower watch. Capped, so a four-hour ground delay can't drain the
    #    leg's budget asking the same question every fifteen minutes.
    if (not departed and dep and now >= dep + SILENT_DEP_FALLBACK
            and not _col(row, "off_actual_api")):
        tries = int(_col(row, "delay_watch_tries") or 0)
        if tries == 0:
            return "T+20: departure check"
        if tries < MAX_DELAY_WATCH_TRIES and (now - last) >= DELAY_WATCH_GAP:
            return f"still on the ground ({tries} of {MAX_DELAY_WATCH_TRIES - 1})"

    # 6. Cruise checkpoints. Only once the aircraft has ACTUALLY departed —
    #    an anchor alone isn't departure. Anchor on the airline's
    #    wheels-off where we have it, otherwise on what ADS-B saw; without
    #    that fallback a flight departing between ground checks has no
    #    anchor and cruise checks can't start at all.
    anchor = (_parse(_col(row, "off_actual_api")) or _parse(_col(row, "off_observed"))
              or _parse(_col(row, "out_actual_api")))
    if departed and anchor and not _col(row, "on_actual_api"):
        points = _cruise_checkpoints(leg, row, anchor)
        # Take the LATEST checkpoint that's due, not the earliest. When
        # the rate floor holds a query back, the checkpoint it was holding
        # is genuinely skipped rather than queued up and fired late.
        for i in range(len(points) - 1, -1, -1):
            if now >= points[i] and last < points[i]:
                return f"cruise check {i + 1} of {CRUISE_CHECKS}"

    return None


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
        "api_queries_used": int(_col(row, "api_queries_used") or 0) + 1,
        "closeout_tries": int(_col(row, "closeout_tries") or 0)
                          + (1 if reason.startswith("closeout") else 0),
        "fallback_tries": int(_col(row, "fallback_tries") or 0)
                          + (1 if reason.startswith("arrival due") else 0),
        "delay_watch_tries": int(_col(row, "delay_watch_tries") or 0)
                             + (1 if (reason.startswith("T+20")
                                      or reason.startswith("still on the ground")) else 0),
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

    write(user_id, leg_id, once=once, latest=latest, always=always)


def refresh(user_id: int, leg, now: datetime, has_adsb: bool = False,
            touchdown: Optional[datetime] = None, departed: bool = False,
            down: bool = False) -> Optional[str]:
    """Refresh this leg's airline data if the budget rules allow.

    Returns the reason a query was spent, or None. Never raises — an
    unreachable or rejected API must not disturb tracking, which works
    perfectly well without it.
    """
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

    row = get_row(user_id, leg.id)
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
        write(user_id, leg.id, always={
            "last_api_query_at": now.isoformat(),
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
        write(user_id, leg.id, always={
            "last_api_query_at": now.isoformat(),
            "last_api_reason": reason,
            "api_queries_used": int(_col(row, "api_queries_used") or 0) + 1,
        })
        return reason

    _apply(user_id, leg.id, data, raw, now, reason, row)
    print(f"[enrichment] {leg.id}: refreshed ({reason})")
    return reason
