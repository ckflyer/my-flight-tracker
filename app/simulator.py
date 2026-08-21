"""Test mode: rehearse a flight without a real one. (1.6.0)

WHY THIS EXISTS. Every bug this app has shipped was found the same way —
the owner flew a trip, something looked wrong, and the evidence was gone by
the time anyone could look at it. Waiting for a real leg to reproduce a
closure bug costs a duty period and, if AeroAPI is on, real money. This
lets any scenario be run on demand, in minutes, for nothing.

WHAT IT DOES NOT DO, and this is the important part. It does not fake the
app's ANSWERS. The simulator produces one thing and one thing only:
POSITION REPORTS, in exactly the shape `livesource.live_state` returns. It
then hands them to the same `flightmatch.observe`, `flightmatch.evaluate`,
`tags` and `closure.maybe_close` that a real flight goes through. If the
closure logic is wrong, test mode is wrong in the same way and by the same
amount, which is the only property that makes it worth having. A simulator
that wrote `closed = 1` directly would prove nothing.

THREE ISOLATION RULES, each guarding a different way this could do harm:

  1. NEVER SPEND. `flights.simulated = 1` is checked at the top of
     `enrichment.refresh` and `enrichment.backfill_gate_in`, before the API
     key is even read. Beyond the money, a real flight somewhere in the
     world may share an invented callsign, and letting its data into a
     simulated row would mix invention with fact in one place.
  2. NEVER ASK ADS-B. `poller` routes a simulated leg here instead of to
     `live_state`, so no request leaves the box and the shared rate limiter
     is untouched.
  3. NEVER COUNT. Simulated legs are excluded from the gate-in sweep, and
     must be excluded from any logbook or export (N2/N3). A rehearsal is
     not a flight and must never reach a legal record.

ON TIME, AND WHY THERE IS NO SPEED CONTROL. The obvious design is a clock
multiplier. It was rejected: the poller, the card, `get_current_info` and
every stored timestamp run on the real clock, so a leg running at 60x is
judged at one time and displayed at another, and every discrepancy that
produced would be a property of the simulator rather than of the app.

Instead, two honest mechanisms:

  * scenarios use SHORT legs (10-14 minutes of block time), so a full
    gate-to-gate happens while you watch;
  * for the rules that mature on a long clock — the 30-minute long stop,
    the 3-hour backstop — the panel has AGE THIS LEG, which shifts the
    row's stored timestamps backwards. Nothing is faked: the leg genuinely
    has been stopped for 30 minutes as far as every stored value is
    concerned, and the real production rule fires on the real threshold.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .airports import enrich_leg
from .db import get_connection
from .flights import flight_key, get_flight, merge_schedule, write
from .models import FlightLeg

UTC = timezone.utc

# Simulated legs use flight numbers in this block. Chosen because no US
# regional operates four-digit numbers starting 99, so an invented leg can
# never collide with a real one the owner might actually fly — the shared
# flight id is DATE-NUMBER-ORIG-DEST, and a collision would put invented
# positions onto a real flight's row.
SIM_FLIGHT_BASE = 9900


class Scenario:
    """One rehearsal, described as a sequence of phases.

    Each scenario is named for the BUG IT REPRODUCES, not for the flight it
    describes. That is the whole point: "normal" proves the happy path
    still works, and every other one is a failure this app has actually
    shipped and must never ship again.
    """

    def __init__(self, key: str, title: str, description: str, origin: str,
                 destination: str, block_min: int, legs: int = 1,
                 stays_transmitting: bool = True, coverage_loss: bool = False,
                 never_arrives: bool = False):
        self.key = key
        self.title = title
        self.description = description
        self.origin = origin
        self.destination = destination
        self.block_min = block_min
        self.legs = legs
        self.stays_transmitting = stays_transmitting
        self.coverage_loss = coverage_loss
        self.never_arrives = never_arrives


SCENARIOS: List[Scenario] = [
    Scenario(
        "normal", "Normal leg",
        "Gate to gate with clean coverage. The aircraft parks and the "
        "transponder goes quiet, so the leg closes on the short observed "
        "route. Proves the happy path still works.",
        "DFW", "OKC", 12, stays_transmitting=False),
    Scenario(
        "taxi_in_trap", "Taxi-in trap (1.4.0)",
        "Lands, parks, and keeps transmitting — an APU running at the gate. "
        "Every closure route that required silence used to be unreachable "
        "here, so the leg hung in taxi-in forever. Age the leg 30 minutes "
        "to watch the long-stop route close it.",
        "DFW", "OKC", 12, stays_transmitting=True),
    Scenario(
        "coverage_loss", "Coverage lost in cruise",
        "Goes dark mid-flight and is never seen again — no touchdown, no "
        "block-in. Only the backstop can end this leg. Age it 3 hours.",
        "DFW", "ICT", 14, coverage_loss=True),
    Scenario(
        "abandoned", "Blocked in, no airline gate-in (1.5.0)",
        "Blocks in normally, but no airline gate-in ever arrives. Before "
        "1.5.0 the leg stopped being swept 3 hours past its scheduled "
        "arrival and stayed open forever. Age it and watch the closeout "
        "sweep catch it.",
        "DFW", "TUL", 11, stays_transmitting=True),
    Scenario(
        "turn", "Two-leg turn (handover)",
        "An out-and-back. Watch the card hand over to leg 2 the moment "
        "leg 1 is on the ground and leg 2's window opens, instead of "
        "sitting on the finished leg for three hours.",
        "DFW", "OKC", 11, legs=2),
    Scenario(
        "never_departs", "Scheduled, never departs",
        "The time passes and nothing ever gets airborne. Nothing may close "
        "this leg: `has_departed` is the guard that stops the clock alone "
        "being treated as evidence. Proves the guard still holds.",
        "DFW", "SGF", 12, never_arrives=True),
]

SCENARIOS_BY_KEY = {s.key: s for s in SCENARIOS}


# --------------------------------------------------------------- lifecycle
def active_sim_rows() -> List[Any]:
    """Every simulated flight row currently in the database."""
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM flights WHERE simulated = 1 ORDER BY date, dep_time_local"
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()


def is_simulated(leg_id: str) -> bool:
    row = get_flight(leg_id)
    return bool(row is not None and _col(row, "simulated"))


def _col(row, name):
    try:
        return row[name]
    except Exception:
        return None


def start(user_id: int, scenario_key: str,
          now: Optional[datetime] = None) -> List[FlightLeg]:
    """Create the scenario's legs on this pilot's roster, departing shortly.

    Departure is set a few minutes out rather than immediately, so the
    pre-departure phases — the leg becoming current at T-20, the aircraft
    being acquired at the gate — are actually exercised. Starting mid-taxi
    would skip the part where most acquisition bugs live.
    """
    scenario = SCENARIOS_BY_KEY.get(scenario_key)
    if scenario is None:
        return []
    now = now or datetime.now(UTC)
    stop(user_id)                       # only ever one rehearsal at a time

    legs: List[FlightLeg] = []
    cursor = now + timedelta(minutes=4)
    for i in range(scenario.legs):
        origin = scenario.origin if i % 2 == 0 else scenario.destination
        dest = scenario.destination if i % 2 == 0 else scenario.origin
        number = str(SIM_FLIGHT_BASE + i)
        leg = _build_leg(number, origin, dest, cursor, scenario.block_min)
        if leg is None:
            continue
        legs.append(leg)
        # 25 minutes on the ground between legs: longer than the 20-minute
        # window so leg 2 is still `upcoming` when leg 1 lands, which is
        # exactly the moment the handover rule has to get right.
        cursor = cursor + timedelta(minutes=scenario.block_min + 25)

    if not legs:
        return []
    merge_schedule(user_id, legs)
    for leg in legs:
        write(leg.id, always={"simulated": 1, "sim_scenario": scenario.key})
    print(f"[simulator] started '{scenario.key}' with {len(legs)} leg(s)")
    return legs


def _build_leg(number: str, origin: str, dest: str, dep_utc: datetime,
               block_min: int) -> Optional[FlightLeg]:
    """A FlightLeg whose LOCAL times land on the intended UTC instants.

    Scheduled times are stored local to each airport, so they are derived
    from the UTC instant through the airport's own zone rather than being
    written directly. Doing it the other way puts a PHX rehearsal seven
    hours out of its own query window, which is one of the fixture traps
    already recorded in the test notes.
    """
    from .airports import get_airport
    o_info, d_info = get_airport(origin), get_airport(dest)
    if not o_info or not d_info:
        return None
    from zoneinfo import ZoneInfo
    arr_utc = dep_utc + timedelta(minutes=block_min)
    dep_local = dep_utc.astimezone(ZoneInfo(o_info.timezone))
    arr_local = arr_utc.astimezone(ZoneInfo(d_info.timezone))
    leg = FlightLeg(
        id=f"{dep_local.date().isoformat()}-{number}-{origin}-{dest}",
        date=dep_local.date(), flight_number=number,
        origin=origin, destination=dest,
        dep_time_local=dep_local.time().replace(second=0, microsecond=0),
        arr_time_local=arr_local.time().replace(second=0, microsecond=0),
        trip_start=(number == str(SIM_FLIGHT_BASE)),
    )
    enrich_leg(leg)
    return leg


def stop(user_id: int) -> int:
    """Delete every simulated leg and everything hanging off it.

    Deleted OUTRIGHT, not retired. Retention exists to protect a record of
    flights that happened; a rehearsal did not happen, and leaving invented
    rows to age out for a year would put them in front of every future
    logbook query as something to remember to exclude.
    """
    rows = active_sim_rows()
    if not rows:
        return 0
    ids = [r["id"] for r in rows]
    conn = get_connection()
    try:
        for fid in ids:
            conn.execute("DELETE FROM positions WHERE flight_key = ?", (fid,))
            conn.execute("DELETE FROM roster WHERE flight_id = ?", (fid,))
            conn.execute("DELETE FROM flights WHERE id = ?", (fid,))
        conn.commit()
    finally:
        conn.close()
    print(f"[simulator] cleared {len(ids)} simulated leg(s)")
    return len(ids)


# The timestamp columns AGE THIS LEG shifts. Every column here holds "when
# something happened"; none holds a duration or a schedule. Getting that
# distinction wrong is the one way this button could lie — shifting a
# SCHEDULED time would move the leg's window and change which rules apply,
# rather than just making the leg older.
AGEABLE = ["stopped_since", "landing_since", "last_signal_at",
           "out_observed", "off_observed", "on_observed", "in_observed",
           "aircraft_acquired_at", "phase_tag_at", "status_tag_at",
           "last_polled_at", "closed_at"]


def age(leg_id: str, minutes: int) -> bool:
    """Shift this leg's observed timestamps backwards by `minutes`.

    The alternative to a clock multiplier, and honest in a way a multiplier
    is not: nothing is faked and no threshold is lowered. After ageing by
    30, the row genuinely records an aircraft that stopped 30 minutes ago,
    and the real long-stop rule fires on the real 30-minute threshold with
    no knowledge that test mode exists.

    Deliberately does NOT touch `date`, `dep_time_local` or `arr_time_local`
    — see AGEABLE. Those define the leg's window; moving them would change
    which rules are even in play, which is a different experiment.
    """
    row = get_flight(leg_id)
    if row is None or not _col(row, "simulated"):
        return False
    delta = timedelta(minutes=minutes)
    shifted = {}
    for col in AGEABLE:
        raw = _col(row, col)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        shifted[col] = (parsed - delta).isoformat()
    if not shifted:
        return False
    write(leg_id, always=shifted)
    print(f"[simulator] aged {leg_id} by {minutes} min")
    return True


# ---------------------------------------------------------------- the feed
def _interp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def state_for(leg, now: datetime) -> Optional[Dict[str, Any]]:
    """The position report this leg's aircraft would be broadcasting now.

    Same shape `livesource.live_state` returns, because the whole value of
    test mode rests on the poller being unable to tell the difference. The
    only caller is `poller`, and only for a leg already known to be
    simulated.

    Returns None to mean "not on ADS-B right now", which is a real answer
    and the one the coverage-loss scenario is built out of.
    """
    row = get_flight(leg.id)
    if row is None:
        return None
    scenario = SCENARIOS_BY_KEY.get(_col(row, "sim_scenario") or "")
    if scenario is None:
        return None
    o, d = leg.origin_info, leg.dest_info
    if not o or not d:
        return None

    dep = leg.dep_datetime_utc()
    arr = leg.arr_datetime_utc()
    if dep is None or arr is None:
        return None

    # A leg that has been AGED has stored timestamps in the past but a
    # schedule that has not moved, so the aircraft must stay wherever the
    # scenario left it rather than being re-derived from the clock.
    if scenario.never_arrives:
        return _fix(o.lat, o.lon, True, 0, 0, leg)

    block_s = max((arr - dep).total_seconds(), 60.0)
    elapsed = (now - dep).total_seconds()

    # Coverage loss is PERMANENT once it happens. Checked before the phase
    # ladder below rather than inside the airborne branch, because that is
    # where it was first written and the aircraft duly reappeared on the
    # ground at the destination — which made the scenario prove the exact
    # opposite of what it claims, closing on `observed` instead of forcing
    # the backstop. A scenario that quietly tests the wrong thing is worse
    # than no scenario.
    if scenario.coverage_loss and elapsed > block_s * 0.44:
        return None

    # Before pushback: parked at the origin gate, transmitting. This is
    # what lets the hex lock be acquired before departure.
    if elapsed < -60:
        return _fix(o.lat, o.lon, True, 0, 0, leg)
    # Taxi out.
    if elapsed < block_s * 0.10:
        return _fix(_interp(o.lat, d.lat, 0.005), _interp(o.lon, d.lon, 0.005),
                    True, 15, 0, leg)
    # Airborne.
    if elapsed < block_s * 0.86:
        t = (elapsed - block_s * 0.10) / (block_s * 0.76)
        alt = int(1000 + 30000 * math.sin(min(max(t, 0.0), 1.0) * math.pi))
        return _fix(_interp(o.lat, d.lat, t), _interp(o.lon, d.lon, t),
                    False, 430, alt, leg)
    # Rollout, then parked.
    if elapsed < block_s * 0.94:
        return _fix(d.lat, d.lon, True, 45, 0, leg)
    if not scenario.stays_transmitting and elapsed > block_s * 1.10:
        return None                     # transponder off at the gate
    return _fix(d.lat, d.lon, True, 0, 0, leg)


def _fix(lat, lon, on_ground, speed, alt, leg) -> Dict[str, Any]:
    """One position report.

    `registration` and `type_code` are deliberately obvious inventions. A
    plausible-looking tail number on a screenshot is how a rehearsal ends
    up in a bug report as though it were a real flight.
    """
    return {
        "lat": lat, "lon": lon, "on_ground": on_ground,
        "altitude_ft": alt, "speed_kts": speed, "track": 0.0,
        "registration": "N0SIM", "type_code": "SIM",
        "aircraft_type": "Simulated aircraft",
        "squawk": "1200",
        # Its own ICAO hex per leg, so the aircraft lock behaves as it does
        # on a real turn: one airframe per leg id, stable across sweeps.
        "icao24": f"51m{abs(hash(flight_key(leg.id))) % 1000:03d}",
        "position_age_s": 2.0, "source": "simulator",
    }
