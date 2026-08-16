"""Which carrier's callsign a bare flight number belongs to.

WHY THIS FILE EXISTS (v7.5)
---------------------------
Callsign prefixes used to be hard-coded in `carrier.py` and `models.py` with
one specific regional's code as the literal default. That was two different
things wearing one hat:

  * BRANDING — naming a carrier in the product. Removed in v7.5.
  * CONFIGURATION — which ICAO prefix the operator of THIS installation
    files under, and which related carriers their deadheads land on. That
    is load-bearing: strip it and deadhead resolution stops working, because
    looking up the wrong prefix finds a flight that does not exist.

So the values did not go away, they moved here and became configurable. A
crew member at a different airline sets two environment variables and the
app works for them. Nobody has to edit code, and no carrier is named in the
user interface.

INVARIANT: `HOME_PREFIX` must appear in `CANDIDATE_PREFIXES`, and first.
It is the common case and ending the search on the first try is the whole
point of ordering them.

Set in docker-compose.yml:

    - PT_HOME_CALLSIGN=ENY
    - PT_CANDIDATE_CALLSIGNS=ENY,AAL,JIA,PDT

Both are optional. The defaults below preserve the behaviour every version
before v7.5 had, so an existing install upgrades to identical behaviour.
"""
from __future__ import annotations

import os
from typing import List

# The ICAO prefix this installation's own legs broadcast under. A bare
# flight number off a bid line is assumed to be this carrier.
HOME_PREFIX: str = (os.environ.get("PT_HOME_CALLSIGN") or "ENY").strip().upper()

# Prefixes a deadhead realistically lands on: the mainline this operator
# feeds plus its sibling regionals. Ordered — cheapest guess first.
_DEFAULT_CANDIDATES = "ENY,AAL,JIA,PDT"


def _load_candidates() -> List[str]:
    raw = os.environ.get("PT_CANDIDATE_CALLSIGNS") or _DEFAULT_CANDIDATES
    out: List[str] = []
    for part in raw.split(","):
        code = part.strip().upper()
        if code and code not in out:
            out.append(code)
    # Enforce the invariant rather than trusting the environment: a typo in
    # compose that dropped the home prefix would make every one of the
    # pilot's OWN legs unresolvable, which is a silent, total failure.
    if HOME_PREFIX and HOME_PREFIX not in out:
        out.insert(0, HOME_PREFIX)
    elif HOME_PREFIX and out and out[0] != HOME_PREFIX:
        out.remove(HOME_PREFIX)
        out.insert(0, HOME_PREFIX)
    return out


CANDIDATE_PREFIXES: List[str] = _load_candidates()


def home_callsign(flight_number: str) -> str:
    """The callsign a bare bid-line flight number broadcasts under."""
    return f"{HOME_PREFIX}{flight_number}"
