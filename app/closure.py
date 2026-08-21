"""Closing a leg out, once and for good.

Closure is one decision with one recorded reason. Once closed, a leg is
FROZEN: no polling, no live data, no recomputation, so a past flight's
numbers never drift as late data trickles in. `closed_by` records WHICH
source ended it, so the card can say "arrived 4:11 (airline)" versus
"~4:11 (observed)" instead of presenting a guess as fact.

FOUR WAYS A LEG CLOSES, most authoritative first
------------------------------------------------
  airline    the airline's own gate-in, or a cancellation
  relaunch   the aircraft took off AGAIN. Unambiguous, free, no timers
  observed   we watched it land, stop, and go quiet
  backstop   nothing left to learn, and well past arrival

TWO BUGS THIS VERSION FIXES, both found by the pilot
-----------------------------------------------------
1. THE BACKSTOP COULD FIRE ON A DELAYED FLIGHT BEFORE IT EVEN LEFT.
   It anchored on the best available arrival estimate, but with no airline
   data that fell back to the SCHEDULED arrival — so a three-hour delay
   meant the backstop clock expired right around the time he actually
   pushed. Worse, its "has it gone quiet?" test passed when there had been
   no signal EVER, which is exactly the case at an outstation with no
   receiver. A delayed flight from a small field could close itself before
   it left the gate.

   Fixed two ways. The backstop cannot start counting until the flight has
   demonstrably BEGUN — ADS-B saw it airborne, or the airline published a
   gate-out or wheels-off. Before that there is no clock at all. And when
   there is no revised arrival to anchor on, it anchors on the observed
   departure plus the scheduled block time, never on the original
   timetable.

2. AN OBSERVED ARRIVAL COULD BE READ OFF AN AIRCRAFT THAT NEVER MOVED.
   Stationary-and-quiet is only meaningful AFTER a landing. Sitting at the
   departure gate with the transponder off looks identical. The observed
   route now requires wheels-on to be known first, from either source.

WHY OBSERVED NOW CLOSES THE LEG EVEN WITH AN API KEY
-----------------------------------------------------
It didn't in v4: only the airline's actual_in could close a leg for a
pilot with a key, which is why closeout hung. The app would watch the
aeroplane park, then ask FlightAware every ten minutes for an hour and a
half, then wait three hours more. actual_in is the OOOI field most often
missing entirely.

By the pilot's own call, a confirmed landing followed by five minutes
stopped and eight minutes silent is good enough to say Arrived. It is
recorded as `observed`, and a late gate-in still UPGRADES it to the
airline's figure and time. Nothing is lost; the card just stops lying
about a flight that visibly finished.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from . import debuglog
from .flights import get_flight, write
from .tags import PHASE_ARRIVED, STATUS_CANCELLED, STATUS_DIVERTED

# An aircraft that has actually blocked in goes quiet — engines and
# transponder off. One holding off-gate waiting for a stand can sit
# stationary for half an hour and keep transmitting the whole time. So a
# stop only counts as arrival when the signal has ALSO gone away.
STOPPED_MIN = timedelta(minutes=5)
SIGNAL_GONE_MIN = timedelta(minutes=8)

# A stop this long after landing closes the leg REGARDLESS of whether the
# transponder is still transmitting. See the LONG STOP branch in decide()
# for why: without it, an aircraft parked at the gate with its transponder
# on can never close, and blocks every leg behind it.
STOPPED_LONG = timedelta(minutes=30)

# Last resort. Deliberately hard to reach — see the module docstring.
BACKSTOP_AFTER_ARRIVAL = timedelta(hours=3)

SOURCE_AIRLINE = "airline"
SOURCE_CANCELLED = "cancelled"
SOURCE_RELAUNCH = "relaunch"
SOURCE_OBSERVED = "observed"
SOURCE_BACKSTOP = "backstop"

# An airline gate-in may replace any of these after the fact.
UPGRADEABLE = {SOURCE_RELAUNCH, SOURCE_OBSERVED, SOURCE_BACKSTOP}

# How long a closed flight stays visible before the 30-day purge takes it.
KEEP_DAYS = 30


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


def has_departed(row) -> bool:
    """Real evidence the flight BEGAN. Not the clock.

    This gates both the backstop and the observed route. Scheduled
    departure passing is not evidence of anything — that assumption is
    what let a delayed flight close itself.
    """
    if row is None:
        return False
    return bool(
        _col(row, "airborne_seen")
        or _col(row, "off_actual_api")
        or _col(row, "out_actual_api")
    )


def is_down(row) -> bool:
    """Has this aircraft actually landed? Either source will do."""
    if row is None:
        return False
    return bool(_col(row, "landed_seen") or _col(row, "on_actual_api"))


def reference_arrival(row, leg) -> Optional[datetime]:
    """The best current estimate of when this flight actually gets in.

    Anchoring the backstop to the SCHEDULED arrival would close a flight
    that is simply running very late — six hours late is a real and normal
    thing. In order of preference: the airline's own figures, then the
    observed departure projected forward by the scheduled block time, and
    only as a last resort the original timetable.
    """
    for key in ("in_actual_api", "in_estimated", "on_actual_api", "on_estimated"):
        parsed = _parse(_col(row, key))
        if parsed:
            return parsed

    # No airline figure. Project the scheduled block time forward from
    # when it ACTUALLY left, so a three-hour delay moves the backstop
    # three hours too instead of leaving it anchored to a plan everyone
    # already knows is wrong.
    departed_at = (_parse(_col(row, "off_actual_api"))
                   or _parse(_col(row, "off_observed"))
                   or _parse(_col(row, "out_actual_api")))
    dep_sched, arr_sched = leg.dep_datetime_utc(), leg.arr_datetime_utc()
    if departed_at and dep_sched and arr_sched and arr_sched > dep_sched:
        return departed_at + (arr_sched - dep_sched)
    return arr_sched


def decide(row, leg, now: datetime,
           observed_in: Optional[datetime] = None,
           stopped_for: Optional[float] = None,
           signal_gap: Optional[float] = None,
           enrichment_fresh: bool = False) -> Optional[Tuple[str, datetime]]:
    """Should this leg close, and on whose authority? (source, when)."""
    if _col(row, "cancelled"):
        return (SOURCE_CANCELLED, now)
    actual_in = _parse(_col(row, "in_actual_api"))
    if actual_in:
        return (SOURCE_AIRLINE, actual_in)

    # The aircraft is flying again — whatever the airline has published,
    # this leg is finished.
    if _col(row, "relaunched"):
        return (SOURCE_RELAUNCH, now)

    # Nothing below may fire until the flight demonstrably began. This is
    # the guard that stops a delayed flight closing itself at the gate.
    if not has_departed(row):
        return None

    # An observed arrival needs THREE things: it must have landed, been
    # stationary long enough, and gone silent. Any two without the third
    # are ambiguous — a stop on its own is just as likely to be waiting
    # for a gate, and silence on its own is just a coverage hole.
    blocked_in = (
        is_down(row)
        and observed_in is not None
        and stopped_for is not None and stopped_for >= STOPPED_MIN.total_seconds()
        and signal_gap is not None and signal_gap >= SIGNAL_GONE_MIN.total_seconds()
    )
    if blocked_in:
        return (SOURCE_OBSERVED, observed_in)

    # LONG STOP (v1.4.0). The route above requires the transponder to go
    # QUIET, and so does the backstop below. That left a hole with no exit:
    #
    #   land -> taxi in -> park at the gate -> KEEP TRANSMITTING
    #
    # which is ordinary behaviour, especially with the APU running or on a
    # quick turn. `signal_gap` never reaches SIGNAL_GONE_MIN, so `observed`
    # cannot fire; `quiet` is false, so the backstop cannot fire either. The
    # only remaining exits were an airline gate-in — the OOOI field most
    # often missing — and `relaunch`, which on the last leg of a day does not
    # happen until the following morning.
    #
    # Result: the leg sat in taxi-in indefinitely and, because it never
    # closed, the next leg never became current. The whole app appeared
    # frozen on a flight that had plainly finished.
    #
    # A stop this long IS the evidence. The five-minute rule was paired with
    # silence to tell "parked" from "holding for a gate", which is fair at
    # five minutes and no longer fair at thirty — a hold that long while
    # perfectly stationary is rare, and when it does happen, closing early
    # is much the smaller error. The alternative is what was actually
    # happening: a finished leg blocking every leg behind it.
    if (is_down(row) and observed_in is not None
            and stopped_for is not None
            and stopped_for >= STOPPED_LONG.total_seconds()):
        return (SOURCE_OBSERVED, observed_in)

    # Backstop. Only when there is nothing left to learn — no live signal,
    # no fresh airline data — and well past the REVISED arrival.
    ref = reference_arrival(row, leg)
    quiet = signal_gap is None or signal_gap >= SIGNAL_GONE_MIN.total_seconds()
    if (ref and now >= ref + BACKSTOP_AFTER_ARRIVAL
            and quiet and not enrichment_fresh):
        return (SOURCE_BACKSTOP, observed_in or now)
    return None


def _arrival_source(row, closed_by: str) -> str:
    if closed_by == SOURCE_AIRLINE or _col(row, "in_actual_api"):
        return "airline"
    if _col(row, "in_observed"):
        return "observed"
    return "estimated"


def close(leg, row, closed_by: str, closed_at: datetime,
          observed_in: Optional[datetime]) -> None:
    """Freeze the leg. The values are already in the row; this just marks
    it finished, records who said so, and sets the purge date."""
    status = None
    if closed_by == SOURCE_CANCELLED or _col(row, "cancelled"):
        status = STATUS_CANCELLED
    elif _col(row, "diverted"):
        status = STATUS_DIVERTED
    write(
        leg.id,
        once={"in_observed": observed_in.isoformat() if observed_in else None},
        always={
            "closed": 1,
            "closed_at": closed_at.isoformat(),
            "closed_by": closed_by,
            "arrival_source": _arrival_source(row, closed_by),
            "purge_after": (closed_at + timedelta(days=KEEP_DAYS)).isoformat(),
            # Progress and time-to-go stop meaning anything on a finished
            # flight, so they are cleared rather than frozen at whatever
            # the last poll happened to compute. A past flight shows no
            # bar and no countdown; the Arrived pill says what happened.
            "ete_min": None,
            "distance_nm": None,
            "progress_pct": None,
            # Closure is the ONLY thing that produces Arrived. The phase
            # machine never invents it — "the aircraft stopped" and "the
            # flight is over" are different claims, and conflating them is
            # what made Arrived fire on a taxi-queue stop in early v2.
            "phase_tag": PHASE_ARRIVED,
            "phase_tag_at": closed_at.isoformat(),
        },
        latest={"status_tag": status} if status else None,
    )
    print(f"[closure] {leg.id}: closed by {closed_by} at {closed_at.isoformat()}")


def maybe_close(leg, row, now: datetime,
                observed_in: Optional[datetime] = None,
                stopped_for: Optional[float] = None,
                signal_gap: Optional[float] = None,
                enrichment_fresh: bool = False) -> Optional[str]:
    """Close the leg if it's time. Returns the source, or None.

    An already-closed leg is left alone, EXCEPT that a late airline
    gate-in may upgrade a provisional close — the airline's own number is
    worth having even if it turns up an hour after we gave up waiting.
    """
    if row is None:
        return None

    if _col(row, "closed"):
        actual_in = _parse(_col(row, "in_actual_api"))
        if actual_in and _col(row, "closed_by") in UPGRADEABLE:
            close(leg, row, SOURCE_AIRLINE, actual_in, observed_in)
            print(f"[closure] {leg.id}: upgraded to airline gate-in")
            return SOURCE_AIRLINE
        return None

    debuglog.log(
        "closure.inputs", subject=getattr(leg, "id", None),
        flight=getattr(leg, "flight_number", None),
        departed=has_departed(row), down=is_down(row),
        relaunched=bool(_col(row, "relaunched")),
        actual_in=_col(row, "in_actual_api"),
        stopped_for_s=stopped_for, signal_gap_s=signal_gap,
        enrichment_fresh=enrichment_fresh,
        # The three thresholds, logged alongside the values they are
        # compared against, so a near-miss is visible without opening code.
        need_stopped_s=STOPPED_MIN.total_seconds(),
        need_silent_s=SIGNAL_GONE_MIN.total_seconds(),
        need_long_stop_s=STOPPED_LONG.total_seconds(),
    )
    verdict = decide(row, leg, now, observed_in=observed_in,
                     stopped_for=stopped_for, signal_gap=signal_gap,
                     enrichment_fresh=enrichment_fresh)
    if not verdict:
        return None
    closed_by, closed_at = verdict
    debuglog.log("closure.decided", subject=getattr(leg, "id", None),
                 closed_by=closed_by, closed_at=closed_at)
    close(leg, row, closed_by, closed_at, observed_in)
    return closed_by


def any_closed(leg_id: str) -> bool:
    """Closure is a fact about the flight, so with a shared row this is
    simply whether it is closed. Kept as a name because callers read
    better for it."""
    return is_closed(leg_id)


def is_closed(leg_id: str) -> bool:
    row = get_flight(leg_id)
    return bool(row is not None and _col(row, "closed"))
