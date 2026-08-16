"""Working out what a paste would actually CHANGE. (N1, 1.5.0)

Before this, importing a schedule replaced the roster: any leg not in the
new paste had its roster row deleted. Pasting September erased August.
Flight ROWS were never the problem — they are shared and adopted, never
duplicated — but the LINK from a pilot to a flight was pruned, and that
link is what the tracker, the calendar and (soon) the logbook read.

Two rules make this safe, and they are different rules for different
reasons:

  SCOPE IS THE MONTH THE PASTE COVERS. A bid line is published a month at
  a time, so a September paste is a statement about September and says
  nothing whatever about August. Reconciling outside its own months would
  let a partial paste delete a whole month it never mentioned.

  ONLY THE FUTURE IS RECONCILED. A re-paste is the pilot correcting what
  is COMING. A leg that already departed happened, and an import must not
  be able to revise history — that is the difference between a schedule
  and a logbook.

  And nothing is applied silently. This module only DESCRIBES the change;
  `main.admin_import_confirm` applies whatever the pilot approves. The
  diff is the only place two invisible failures can be caught: a trip
  dropped from the line that the pilot forgot to remove, and a leg flown
  that was never on the line at all.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from .flights import flight_key
from .models import FlightLeg

# What the pilot is shown, in this order. "unchanged" is included on
# purpose: a diff that hides the untouched legs makes the pilot count rows
# to satisfy himself nothing was lost, which is the anxiety the diff exists
# to remove.
ADDED = "added"
REMOVED = "removed"
CHANGED = "changed"
UNCHANGED = "unchanged"


def months_covered(legs: List[FlightLeg]) -> Set[str]:
    """The set of "YYYY-MM" this paste makes a statement about."""
    return {leg.date.strftime("%Y-%m") for leg in legs}


def _shape(leg: FlightLeg) -> Tuple:
    """The fields a re-paste is allowed to correct.

    Route and date are NOT here — they are baked into the flight id, so
    changing either produces a different flight. That is correct: a leg
    that moved to another airport is a different leg, and it shows up as
    one removal and one addition rather than a silent edit.
    """
    return (leg.dep_time_local.isoformat(),
            leg.arr_time_local.isoformat(),
            bool(leg.is_deadhead))


def _departed(leg: FlightLeg, now: datetime) -> bool:
    """Has this leg's scheduled departure passed?

    Resolved through the airport's zone (models does this via
    timezones.py), never by comparing local clock times. On the rare leg
    whose airport will not resolve, treat it as PAST — the safe direction,
    because the consequence of being wrong is that an import declines to
    delete something rather than deleting something it should not have.
    """
    dep = leg.dep_datetime_utc()
    return True if dep is None else dep <= now


def build_diff(pasted: List[FlightLeg], current: List[FlightLeg],
               now: Optional[datetime] = None) -> Dict[str, List[Dict]]:
    """Categorise every leg the pilot needs to see before approving.

    Returns four lists of {"leg": FlightLeg, ...}, plus the months in
    scope. Removals carry `was` so the page can say what is going.
    """
    now = now or datetime.now(timezone.utc)
    scope = months_covered(pasted)

    paste_by_id = {flight_key(l.id): l for l in pasted}
    current_by_id = {flight_key(l.id): l for l in current}

    out: Dict[str, List[Dict]] = {ADDED: [], REMOVED: [], CHANGED: [],
                                  UNCHANGED: []}

    for fid, leg in paste_by_id.items():
        have = current_by_id.get(fid)
        if have is None:
            out[ADDED].append({"leg": leg})
        elif _shape(have) != _shape(leg):
            out[CHANGED].append({"leg": leg, "was": have})
        else:
            out[UNCHANGED].append({"leg": leg})

    # The two guards. A roster leg is only ever a candidate for removal if
    # the paste covers its month AND it has not departed yet.
    for fid, leg in current_by_id.items():
        if fid in paste_by_id:
            continue
        if leg.date.strftime("%Y-%m") not in scope:
            continue          # different month; this paste says nothing
        if _departed(leg, now):
            continue          # already flown; history is not revisable
        out[REMOVED].append({"leg": leg})

    for key in out:
        out[key].sort(key=lambda e: (e["leg"].date,
                                     e["leg"].dep_time_local))
    return out


def month_labels(months: Set[str]) -> str:
    """"August 2026", or "August–September 2026" for a paste that spans."""
    if not months:
        return ""
    parsed = sorted(datetime.strptime(m, "%Y-%m") for m in months)
    if len(parsed) == 1:
        return parsed[0].strftime("%B %Y")
    if parsed[0].year == parsed[-1].year:
        return f"{parsed[0].strftime('%B')}–{parsed[-1].strftime('%B %Y')}"
    return f"{parsed[0].strftime('%B %Y')}–{parsed[-1].strftime('%B %Y')}"
