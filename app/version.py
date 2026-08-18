"""The four version numbers this app carries, and why they are four.

They get confused constantly, so each one is named for the question it
answers. If you are adding a fifth, you are probably conflating two of these.

    VERSION             which BUILD is running          1.0.0
    API_VERSION         what SHAPE the JSON is          1
    SCHEMA_VERSION      what SHAPE the DATABASE is      1
    MIN_CLIENT_VERSION  oldest BUILD still supported    1.0.0

They move independently. A week of UI work bumps VERSION three times and
touches none of the others.

-----------------------------------------------------------------------------
WHY THE OLD SCHEME WAS REPLACED (v7.4 -> 1.0.0)
-----------------------------------------------------------------------------
Versions used to be a single decimal: 5.5, 6.3, 7.4. Two problems, one fatal.

The fatal one: 7.9 is followed by 7.10, and "7.10" sorts BEFORE "7.9" in
every string comparison and equals 7.1 in every numeric one. Anything that
ever compares versions -- an update prompt, a migration guard, a minimum
client check -- silently reads the newer build as older. There is no way to
patch around this after the fact; the numbers themselves are ambiguous.

The lesser one: a single number cannot say how big a change is. "6.3 -> 6.4"
gave no hint whether that was a colour tweak or a database rebuild, which is
exactly what a person deciding whether to back up first needs to know.

So: semantic versioning, MAJOR.MINOR.PATCH, restarting at 1.0.0 with the
MyPilot rebrand. 1.0.0 rather than 0.1.0 because the thing is already flying
real trips for real families with 400 tests behind it -- calling that a
pre-release would be false modesty that misleads anyone reading the number.

    MAJOR  a break. Data migrates one way and cannot migrate back, or an
           old client stops working. Back up before deploying one.
    MINOR  a new capability, nothing existing breaks. The common case.
    PATCH  a fix. No new behaviour, no new data.

Ordering rule: compare field by field as INTEGERS, never as text and never
as a float. 1.10.0 is newer than 1.9.0. Use version_tuple() below; do not
hand-roll the comparison at the call site.
-----------------------------------------------------------------------------
"""
from __future__ import annotations

from typing import Tuple

# Bump on EVERY build. This also keys the service worker cache (static/sw.js),
# so forgetting means phones keep serving the previous build's CSS and
# JavaScript and `update.sh` appears to do nothing at all.
VERSION = "1.14.1"

# The JSON contract. Routes mount at /api/v{API_VERSION}/. An integer, not a
# semver, because there is nothing to express beyond "which contract" -- it is
# a namespace, not a measurement.
#
# Bump ONLY on a break: a removed field, a renamed field, a changed type.
# Adding a field is not a break. When it moves, the previous prefix KEEPS
# SERVING THE OLD SHAPE until nothing calls it, because an installed app on
# someone's phone cannot be updated from here.
API_VERSION = 1

# The database shape. Recorded in the `meta` table so a database can state
# what it is rather than being guessed at by inspecting columns.
#
# Migrations are append-only and idempotent (see db.py). This number exists
# so they can also be ORDERED and SKIPPED: a v1 database knows it needs
# migrations 2..N, and a database from the future can refuse to be opened by
# an old build instead of being quietly corrupted by it.
SCHEMA_VERSION = 1

# The oldest app build the server still accepts. Meaningless today, when
# every client is a browser that got its code from this server seconds ago.
# It becomes load-bearing the moment a native app exists, because that client
# may be months old and cannot be reached.
#
# Exposed at /api/v1/meta so a client can check itself on launch and say
# "please update" rather than rendering a blank screen against a contract it
# no longer understands. Raise it only when an old build genuinely cannot
# work -- it force-updates people, and doing that casually trains them to
# dread the app.
MIN_CLIENT_VERSION = "1.0.0"


def version_tuple(v: str) -> Tuple[int, ...]:
    """Parse a semver string into integers for comparison.

    Tolerant on purpose: a client sends its own version string, and a
    malformed one from an old or hostile build must not raise inside a
    request handler. Unparseable parts read as 0, so a garbage version
    compares as very old and gets told to update -- the safe direction.
    """
    parts = []
    for chunk in str(v).split(".")[:3]:
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def client_is_supported(client_version: str) -> bool:
    """Is a client on this build new enough to talk to this server?"""
    return version_tuple(client_version) >= version_tuple(MIN_CLIENT_VERSION)
