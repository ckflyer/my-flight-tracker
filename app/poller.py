"""The only thing in this app that decides anything.

Every 20 seconds this sweeps every flight inside its window, across all
accounts, and does the whole job in one place: fetch the live state, judge
whether that aircraft really belongs to the leg, record the position,
advance the two tags, spend an AeroAPI query if a trigger says so, and
decide whether the leg is finished. Then it writes the answer down.

WHY THIS RUNS EVEN WHEN NOBODY IS WATCHING
-------------------------------------------
Until v2.6 positions were only written while somebody had the page open,
because recording was reachable only from a request handler. That meant
the pilot — the one person guaranteed NOT to be watching — got no track of
his own flights, and "Arrived" could never be detected on an unwatched
flight.

WHY THE PAGE NO LONGER DOES ANY OF THIS
----------------------------------------
In v4 it did. `compute_live_payload` ran on every page render and every
poll from every browser, and it fetched live data, wrote track points and
advanced the aircraft state machine. So the app had two engines running
the same logic on different clocks, and which one got there first changed
the answer. That is where the ordering bugs came from. Now the page reads
the flight row and renders it; only this decides.

Rate limiting is NOT handled here — livesource owns the shared cache and
the one-request-per-second floor, so the poller and any live viewers
naturally share one upstream request per flight.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from . import tags
from .carrier import needs_resolution, resolve as resolve_carrier
from .closure import maybe_close
from .db import get_connection
from .enrichment import (GATEIN_BACKFILL_TRIES, backfill_gate_in, credentials,
                         payer_for, refresh as refresh_enrichment,
                         refresh_usage)
from .flightmatch import (ACQUIRE_BEFORE_DEP_MINUTES, evaluate, has_flown,
                          observe, observed_gate_in, signal_gap_seconds,
                          stopped_seconds, wheels_down_at)
from .flights import (get_flight, owners_of, purge_old, rows_awaiting_gate_in,
                      row_to_leg, unclosed_rows, write)
from .livesource import live_state
from .schedule import CURRENT_GRACE, get_current_info
from .simulator import state_for as sim_state_for
from .track import record_position
from .view import recompute_derived

# How often to sweep. Matches the viewer's default poll so a recorded
# track has the same resolution whether or not anyone was watching.
INTERVAL_S = int(os.environ.get("TRACK_POLLER_INTERVAL_S", "20"))

# How early a not-yet-current leg gets swept. Five minutes wider than
# enrichment's T-30 first look so that branch is reachable with margin
# rather than depending on a sweep landing exactly on it. Deliberately in
# the poller rather than in get_current_info, because "current" also
# drives flight selection, the map and the card, and moving that boundary
# would change all of them.
# DERIVED, not repeated. These two were independent numbers (35 here, 20
# there) and the mismatch created a dead zone where flights were polled but
# could never be acquired. Tying them together means that gap cannot come
# back if either is retuned.
PREVIEW_WINDOW = timedelta(minutes=ACQUIRE_BEFORE_DEP_MINUTES)

# Escape hatch: set TRACK_POLLER_ENABLED=0 to run read-only.
ENABLED = os.environ.get("TRACK_POLLER_ENABLED", "1") not in ("0", "false", "False")

# Airline data this fresh means the flight is still live and the backstop
# must not fire, however late it is running.
ENRICHMENT_FRESH = timedelta(minutes=45)

_thread: threading.Thread | None = None
_started = False
_lock = threading.Lock()
# When the last sweep ran. Read by Settings so the pilot can tell a poller
# that has stopped from one that simply has nothing to do — those look
# identical from the outside, and that ambiguity is what made a 22-hour-old
# usage reading impossible to interpret.
_last_sweep_at: datetime | None = None
_last_purge_at: float = 0.0
PURGE_INTERVAL_S = 6 * 3600.0


def _all_user_ids() -> List[int]:
    conn = get_connection()
    try:
        return [r["id"] for r in conn.execute("SELECT id FROM users ORDER BY id")]
    finally:
        conn.close()


def active_flights(now: Optional[datetime] = None) -> Dict[str, object]:
    """Every flight currently in its window, as {leg_id: FlightLeg}.

    Deduplicated across accounts: several pilots on one flight share a
    single row, so it is fetched once, judged once, and written once.
    """
    found: Dict[str, object] = {}
    now = now or datetime.now(timezone.utc)
    for user_id in _all_user_ids():
        try:
            # ONE clock for the whole sweep. This used to let
            # get_current_info read the wall clock while the preview
            # window below used the sweep's own `now`, so the two could
            # disagree about which leg was current — and every derived
            # time in the sweep was then measured against a different
            # instant than the one that selected the leg.
            info = get_current_info(user_id, now)
        except Exception as e:
            print(f"[poller] schedule lookup failed for user {user_id}: {e}")
            continue
        leg = info.current
        if leg and leg.callsign:
            found.setdefault(leg.id, leg)
        # A leg isn't `current` until T-20, but the first AeroAPI look is
        # due at T-30 — so without this that branch was simply unreachable
        # and no gate, revised arrival or published delay ever arrived
        # before pushback, which is what happened up to v4.4.
        for upcoming in info.upcoming:
            dep = upcoming.dep_datetime_utc()
            if upcoming.callsign and dep and now >= dep - PREVIEW_WINDOW:
                found.setdefault(upcoming.id, upcoming)
    return found


def _update_tags(leg, now: datetime) -> None:
    """Recompute both pills and the derived figures, then store them.

    Runs ONCE per flight, not once per crew member — the row is shared, so
    the tags are too. Phase goes through the forward-only guard, so a
    coverage gap can never walk the flight backwards. Status is free to
    move both ways, except Cancelled and Diverted which stick.
    """
    if True:
        row = get_flight(leg.id)
        if row is None or row["closed"]:
            return
        candidate = tags.compute_phase(row, leg, now)
        phase = tags.advance_phase(row["phase_tag"], candidate)
        status, dep_rev, arr_rev = tags.compute_status(row, leg, now)

        always = dict(recompute_derived(row, leg, now))
        always["status_tag"] = status
        always["dep_revision_min"] = dep_rev
        always["arr_revision_min"] = arr_rev
        always["last_polled_at"] = now.isoformat()
        if phase != row["phase_tag"]:
            always["phase_tag"] = phase
            always["phase_tag_at"] = now.isoformat()
        if status != row["status_tag"]:
            always["status_tag_at"] = now.isoformat()

        # The lateness note is computed for display, but the minutes are
        # stored so the calendar and past-flight lists don't each have to
        # re-derive them.
        from .view import _variance
        dep_v = _variance(leg.dep_datetime_utc(), row["out_actual_api"],
                          row["out_observed"], None, None, "24", "", "")
        arr_v = _variance(leg.arr_datetime_utc(), row["in_actual_api"],
                          row["in_observed"], None, None, "24", "", "")
        always["out_variance_min"] = (dep_v or {}).get("minutes")
        always["in_variance_min"] = (arr_v or {}).get("minutes")

        write(leg.id, always=always)


def _settle(leg, now: datetime, has_adsb: bool) -> None:
    """Spend a query if a trigger says so, then decide whether it's over.

    Runs per owner: each pilot's own key pays for their own airline data
    and each keeps their own closeout record.
    """
    shared = get_flight(leg.id)
    touchdown = wheels_down_at(shared)
    departed = has_flown(shared)
    observed_in = observed_gate_in(shared)
    stopped_for = stopped_seconds(shared, now)
    signal_gap = signal_gap_seconds(shared, now)
    # "Down" gates the closeout pass: only worth hunting for gate-in once
    # the aircraft has actually landed somewhere.
    down = touchdown is not None

    if shared is None or shared["closed"]:
        return

    # ONE query per flight, however many crew are on it. That is the point
    # of the shared row: in v5.0 a captain and an FO on the same leg each
    # paid their own key for an identical answer.
    payer = payer_for(leg.id)
    if payer is not None:
        try:
            refresh_enrichment(payer, leg, now, has_adsb=has_adsb,
                               touchdown=touchdown, departed=departed, down=down)
        except Exception as e:
            print(f"[poller] enrichment failed for {leg.id}: {e}")

    # Usage is refreshed for EVERY user once per sweep in poll_once, not
    # here — doing it per active leg meant it never ran on a day off.

    try:
        row = get_flight(leg.id)
        last_api = row["last_api_query_at"]
        fresh = False
        if last_api:
            try:
                fresh = (now - datetime.fromisoformat(last_api)) < ENRICHMENT_FRESH
            except Exception:
                fresh = False
        maybe_close(leg, row, now, observed_in=observed_in,
                    stopped_for=stopped_for, signal_gap=signal_gap,
                    enrichment_fresh=fresh)
    except Exception as e:
        print(f"[poller] closeout failed for {leg.id}: {e}")


def _refresh_all_usage(now: datetime) -> None:
    """Pull every key's free spend figure, whether or not anyone is flying.

    This used to live only inside _settle, which runs per ACTIVE LEG. So on
    a day off — or any of the 20-odd hours a day with no flight in its
    window — nothing asked, and the Settings page showed a reading 22 hours
    old. Worse than cosmetic: budget_state treats a reading older than
    USAGE_STALE_AFTER as stale and falls back to the local count, so the
    figure the cap is enforced against went stale precisely when nothing
    was happening to refresh it.

    The call is free and throttled inside refresh_usage, so sweeping every
    user every 20 seconds costs one HTTP request per key per 15 minutes.
    """
    for user_id in _all_user_ids():
        try:
            refresh_usage(user_id, now)
        except Exception as e:
            print(f"[poller] usage refresh failed for {user_id}: {e}")


def _closeout_sweep(now: datetime) -> int:
    """Judge legs the ordinary sweep has already let go of. (1.5.0)

    THE HOLE THIS FILLS. `active_flights()` returns the CURRENT leg and
    imminent upcoming ones, and nothing else. `get_current_info` releases a
    leg 3 hours past its SCHEDULED arrival unless it is demonstrably still
    airborne. After that, nothing in the app ever looked at it again.

    Closure was written on the opposite assumption. Its backstop matures 3
    hours after the REVISED arrival — later than the scheduled one on every
    late flight, which is precisely the population the backstop exists for.
    `relaunch` needs a sweep to notice the aircraft flying again. The
    1.4.0 long-stop route needs a sweep at the 30-minute mark. All three
    quietly stopped being reachable at the same instant, and a leg that
    blocked in without an airline gate-in simply stayed open forever.

    This costs NOTHING and is deliberately kept that way: every value
    `closure.decide` reads is already stored on the row, so this is pure
    re-evaluation with no ADS-B call and no AeroAPI call. `stopped_since`
    and `last_signal_at` are timestamps, so the stopped-for and
    signal-gap figures keep growing correctly on their own without anyone
    fetching anything.

    Note the ordering against the live sweep: a leg still inside its
    window is being polled properly and is skipped here, so the two can
    never both judge the same leg in one pass.
    """
    closed = 0
    for row in unclosed_rows(now):
        try:
            leg = row_to_leg(row)
            if leg is None:
                continue
            arr = leg.arr_datetime_utc()
            # Leave anything still inside the live window alone — it has a
            # real sweep with live data, and that one is better informed.
            if arr and now < arr + CURRENT_GRACE:
                continue
            if maybe_close(leg, row, now,
                           observed_in=observed_gate_in(row),
                           stopped_for=stopped_seconds(row, now),
                           signal_gap=signal_gap_seconds(row, now),
                           enrichment_fresh=False):
                closed += 1
                _update_tags(leg, now)
        except Exception as e:
            print(f"[poller] closeout sweep failed for {row['id']}: {e}")
    return closed


def _gate_in_sweep(now: datetime) -> int:
    """Chase the airline gate-in on legs that closed without one. (1.5.0)

    `closure.maybe_close` has always been able to UPGRADE an observed or
    backstop close to the airline's own figure. It could never happen,
    because a closed leg was never polled again and `should_query` refuses
    to spend on one — the upgrade was a door with a wall behind it.

    This is the only paid part of the two new sweeps, and it is small: at
    most three queries, on only those legs that finished without an
    airline gate-in, spaced out over roughly a day. It matters more for
    the logbook (N3) than for the tracker, because `in_actual_api` is what
    a logbook export is allowed to use — an observed time must never
    masquerade as a reported one in a legal record.
    """
    upgraded = 0
    for row in rows_awaiting_gate_in(now, max_tries=GATEIN_BACKFILL_TRIES):
        try:
            leg = row_to_leg(row)
            if leg is None:
                continue
            payer = payer_for(leg.id)
            if payer is None:
                continue
            if not backfill_gate_in(payer, leg, now):
                continue
            # Re-read: the query may have just landed the gate-in, which
            # is what makes the upgrade path reachable at last.
            fresh = get_flight(leg.id)
            if maybe_close(leg, fresh, now):
                upgraded += 1
        except Exception as e:
            print(f"[poller] gate-in sweep failed for {row['id']}: {e}")
    return upgraded


def poll_once(now: Optional[datetime] = None) -> int:
    """One sweep. Returns how many flights had a position recorded."""
    global _last_sweep_at
    recorded = 0
    now = now or datetime.now(timezone.utc)
    _last_sweep_at = now
    _refresh_all_usage(now)
    for leg_id, leg in active_flights(now).items():
        try:
            # A deadhead's FFDO line has no carrier, so the callsign has
            # to be resolved before anything is looked up — searching for
            # ENY4110 when the aircraft squawks AAL4110 finds nothing.
            # Resolved once and stored on the row, not every sweep.
            if needs_resolution(leg):
                # payer_for, not "anyone with a key" — this path bills the
                # same wallet as everything else, so it has to respect the
                # same cap. Before v5.2 it looked up a key directly and
                # spent regardless of budget. A None payer just means the
                # free ADS-B probe gets its turn and nothing is charged.
                payer = payer_for(leg_id)
                try:
                    resolve_carrier(leg, credentials(payer) if payer else None,
                                    now, user_id=payer)
                except Exception as e:
                    print(f"[poller] carrier resolution failed for {leg_id}: {e}")

            row = get_flight(leg_id)
            if row is not None and row["closed"]:
                continue

            state = None
            try:
                # A simulated leg gets its positions from simulator.py and
                # NEVER touches the network. Routed here rather than inside
                # live_state so the shared ADS-B cache and its 1 req/s
                # floor stay entirely untouched by test mode.
                if row is not None and row["simulated"]:
                    state = sim_state_for(leg, now)
                else:
                    state = live_state(leg.callsign)
            except Exception as e:
                print(f"[poller] lookup failed for {leg.callsign}: {e}")

            has_adsb = False
            if state is not None:
                verdict = evaluate(leg, state, now, row=row)
                if not verdict.accepted:
                    print(f"[poller] ignoring aircraft on {leg.callsign} "
                          f"for {leg_id}: {verdict.reason}")
                else:
                    # Re-read: evaluate() may have just acquired the hex.
                    observe(leg, state, now, row=get_flight(leg_id))
                    if state.get("lat") is not None and state.get("lon") is not None:
                        record_position(leg_id, state["lat"], state["lon"], now,
                                        state.get("on_ground"))
                        recorded += 1
                        has_adsb = True

            _update_tags(leg, now)
            # No ADS-B for this leg is precisely when the airline's own
            # OOOI matters most, so enrichment still gets its chance.
            _settle(leg, now, has_adsb=has_adsb)
            # Closure may have just fired; make the pills agree with it.
            _update_tags(leg, now)
        except Exception as e:
            print(f"[poller] {leg_id} failed: {e}")

    # AFTER the live sweep, never before: a leg inside its window is
    # better judged with live data, and these two deliberately skip
    # anything the loop above has already handled this pass.
    try:
        _closeout_sweep(now)
    except Exception as e:
        print(f"[poller] closeout sweep failed: {e}")
    try:
        _gate_in_sweep(now)
    except Exception as e:
        print(f"[poller] gate-in sweep failed: {e}")
    return recorded


def _maybe_purge() -> None:
    global _last_purge_at
    if time.monotonic() - _last_purge_at < PURGE_INTERVAL_S:
        return
    _last_purge_at = time.monotonic()
    try:
        removed = purge_old()
        if removed:
            print(f"[poller] purged {removed} flights past their 30-day retention")
    except Exception as e:
        print(f"[poller] purge failed: {e}")


def _loop() -> None:
    print(f"[poller] running every {INTERVAL_S}s")
    while True:
        try:
            poll_once()
            _maybe_purge()
        except Exception as e:
            # Never let one bad sweep kill the thread — a poller that dies
            # silently looks exactly like the bug it replaced.
            print(f"[poller] sweep failed: {e}")
        time.sleep(INTERVAL_S)


def start() -> bool:
    """Start the poller once per process. Safe to call repeatedly."""
    global _thread, _started
    if not ENABLED:
        print("[poller] disabled via TRACK_POLLER_ENABLED=0")
        return False
    with _lock:
        if _started:
            return False
        _thread = threading.Thread(target=_loop, name="flight-poller", daemon=True)
        _thread.start()
        _started = True
        return True


def last_sweep_at() -> Optional[datetime]:
    """When the background poller last completed a sweep, or None if it has
    not run yet in this process. Resets on restart, deliberately — "not
    since the container came up" is the answer worth showing."""
    return _last_sweep_at


def is_running() -> bool:
    return bool(_started and _thread is not None and _thread.is_alive())
