"""Wall-clock times to UTC, correctly.

WHY THIS FILE EXISTS
--------------------
A bid line gives WALL-CLOCK times: "0715 DFW, 0912 LFT". No date on the
arrival, no offset, no zone. Everything downstream — is this leg current,
how late is it, what goes in the logbook — depends on turning those into
real instants. Three separate bugs lived in that conversion, all silent:

  1. NONEXISTENT TIMES. On the US spring-forward day the wall clock jumps
     02:00 -> 03:00, so 02:30 never happens. `datetime.combine(d, t,
     tzinfo=tz)` accepts it anyway and produces an instant an hour off. No
     error, no warning.

  2. AMBIGUOUS TIMES. On the fall-back day 01:30 happens TWICE, an hour
     apart. Python's default `fold=0` silently picks the first. Also an
     hour off, and also silent.

  3. CROSS-ZONE COMPARISON. Arrival date was inferred with
     `if arr_time_local < dep_time_local: add a day` — comparing a clock in
     the origin's zone against a clock in the destination's zone as though
     they were the same clock. The code called this a "simple heuristic".
     It is right for most domestic legs BY LUCK, and wrong as soon as the
     zone offsets differ enough, which is exactly what a westbound
     transcon or anything near the date line does.

The fix for (3) is to stop guessing from clock arithmetic entirely. Compute
departure as a real instant, then test each candidate arrival DATE and keep
the first that lands after departure. That is correct for every zone pair
in existence, including the date line, with no special cases.

INVARIANT: nothing outside this module may build a UTC instant from a bid
line's local time. If you find `datetime.combine(...)` with a `tzinfo=`
elsewhere, it is a bug — route it through `local_to_utc`.
"""
from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, time as time_cls, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

UTC = timezone.utc

# The longest a single leg can plausibly block. Used to reject a candidate
# arrival date that would imply an absurd flight rather than the real one on
# the next day. Deliberately generous: it exists to catch a date resolved
# 24 hours wrong, not to police long-haul.
MAX_BLOCK = timedelta(hours=20)


def local_to_utc(d: date_cls, t: time_cls, tz_name: str) -> Optional[datetime]:
    """One wall-clock time in one named zone, as a real UTC instant.

    Handles both DST edges explicitly rather than inheriting Python's
    defaults, because both defaults are wrong for scheduling:

    AMBIGUOUS (clocks go back, the hour repeats). Two real instants match
    this wall time. We take the FIRST, which is `fold=0`. That is what a
    published schedule means: the airline printed 01:30 and the aeroplane
    left the first time the clock read 01:30. This is also Python's default,
    but it is chosen here rather than inherited, so it is visible and
    testable instead of accidental.

    NONEXISTENT (clocks go forward, the hour is skipped). No instant matches
    this wall time at all. A schedule showing 02:30 on that date means the
    event happens after the jump, so we advance past the gap. Returning None
    or raising would be worse: one leg a year would simply vanish.
    """
    if not tz_name:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        return None

    naive = datetime.combine(d, t)
    aware = naive.replace(tzinfo=tz)

    # A wall time that does not survive a round trip through UTC did not
    # exist. This is the only reliable detection: comparing utcoffset at
    # fold 0 and 1 tells you a transition is nearby, not that THIS time
    # fell inside the gap.
    #
    # When it did not exist, fold=0 is already the answer we want. For a
    # skipped hour, fold=0 carries the PRE-transition offset, so converting
    # with it lands the instant AFTER the gap: 02:30 on a spring-forward
    # date resolves to 03:30 local, which is what a schedule printing 02:30
    # actually means. fold=1 would land at 01:30 — before the jump, i.e.
    # earlier than the printed time, which is never right for a departure.
    #
    # So there is no arithmetic to do here. The branch exists to make the
    # choice DELIBERATE and testable rather than an inherited default, and
    # to hold this explanation. An earlier version of this function
    # "corrected" the value by shifting it and produced 01:30 — an hour
    # early, in the one direction that makes a crew member miss a report.
    round_trip = aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
    if round_trip != naive:
        return naive.replace(tzinfo=tz, fold=0).astimezone(UTC)

    return aware.astimezone(UTC)


def resolve_arrival_utc(
    dep_utc: datetime,
    dep_date: date_cls,
    arr_time: time_cls,
    dest_tz: str,
) -> Optional[datetime]:
    """The arrival instant, given a bare arrival clock time and no date.

    A bid line prints "0912" with no indication of whether that is today or
    tomorrow at the destination. Rather than inferring from clock arithmetic
    — which requires both times to share a zone, and they do not — try each
    plausible arrival DATE and keep the first instant that falls after
    departure and inside a believable block time.

    Candidates run -1..+2 days. -1 is not paranoia: a westbound leg can land
    at a local DATE earlier than the departure date (crossing the date line
    the helpful way), and +2 covers a long leg departing late local.
    """
    if dep_utc is None or not dest_tz:
        return None

    best: Optional[datetime] = None
    for offset in (-1, 0, 1, 2):
        candidate = local_to_utc(dep_date + timedelta(days=offset), arr_time, dest_tz)
        if candidate is None:
            continue
        block = candidate - dep_utc
        # Strictly after departure: a leg cannot land before it leaves, and
        # a zero-length block is a resolution failure, not a real flight.
        if block <= timedelta(0) or block > MAX_BLOCK:
            continue
        if best is None or candidate < best:
            best = candidate
    return best


def utc_to_local(dt: Optional[datetime], tz_name: Optional[str]) -> Optional[datetime]:
    """A UTC instant as wall time in a named zone. Safe on None."""
    if dt is None or not tz_name:
        return None
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        return None


def parse_iso_utc(raw: Optional[str]) -> Optional[datetime]:
    """Read a stored timestamp back as an aware UTC datetime.

    Stored values have been written by several generations of this app and
    are not perfectly uniform: some carry a 'Z', some an explicit offset,
    some nothing at all. A naive value is ASSUMED UTC, because that is what
    every writer in this codebase intends — but assuming is why this lives
    in one function instead of being repeated at a dozen call sites.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
