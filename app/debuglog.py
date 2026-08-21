"""Why a flight did what it did.

WHAT THIS IS FOR
----------------
The taxi-in bug that prompted this file took a code read to find, because
nothing anywhere recorded WHY a leg failed to close. The poller ran, the
closure logic said "not yet", and that decision left no trace. From the
outside the app simply looked frozen on a finished flight.

So this is not general application logging. `print()` already covers "the
server started". This records DECISIONS — the handful of judgements that
determine what a family member sees, each with the inputs that produced it,
so a question like "why is it still showing taxi-in" is answered by reading
rather than by reasoning about code.

DESIGN CONSTRAINTS, and why each one
------------------------------------
* SQLite, same database. A file would need rotation, a mount, and a way to
  read it from a phone. The database is already backed up (BACKUP.md) and
  already reachable from the admin page.

* CAPPED, not rotated. The poller runs every few seconds forever, so this
  table grows without bound by construction. It self-trims to MAX_EVENTS on
  insert. Retention here is deliberately NOT tied to PT_RETENTION_DAYS:
  flight history is precious and kept a year, diagnostics are disposable and
  measured in days.

* NEVER RAISES. A logging failure must not break a poll. Every public
  function swallows everything. An app that dies because it could not write
  a debug row is strictly worse than one with no debug rows.

* NO SECRETS. Never pass an API key, session token or share code. There is
  a scrub on the way in as a backstop, but the rule is do not pass them.

READING IT
----------
Admin page, or:

    sqlite3 data/flighttracker.db \\
      "SELECT at, event, subject, detail FROM debug_events
       ORDER BY id DESC LIMIT 40;"
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .db import get_connection

# Roughly a week of ordinary polling. Small enough that the table stays
# inconsequential next to a year of position breadcrumbs.
MAX_EVENTS = int(os.environ.get("PT_DEBUG_MAX_EVENTS") or 20000)

# Off by default. Decision logging is cheap but not free, and somebody
# running this happily has no reason to pay for it.
ENABLED = (os.environ.get("PT_DEBUG_LOG") or "").strip().lower() in {"1", "true", "yes", "on"}

# Trim is O(table) and pointless on every insert.
_TRIM_EVERY = 500
_counter = 0
_lock = threading.Lock()

# Substrings that mean a value must never be written, however it arrived.
# Belt and braces: the rule is not to pass these in the first place.
_SECRET_HINTS = ("key", "token", "secret", "password", "cookie", "code")


def _scrub(detail: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in (detail or {}).items():
        if any(hint in k.lower() for hint in _SECRET_HINTS):
            out[k] = "<redacted>"
        elif isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, timedelta):
            out[k] = round(v.total_seconds(), 1)
        else:
            # Anything else is summarised rather than serialised. A row row
            # or an ORM object would otherwise dump a screenful.
            out[k] = f"<{type(v).__name__}>"
    return out


def init() -> None:
    """Create the table. Safe to call repeatedly."""
    try:
        conn = get_connection()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS debug_events (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    at      TEXT NOT NULL,
                    event   TEXT NOT NULL,
                    subject TEXT,
                    detail  TEXT
                )
                """
            )
            # Reading is always "the most recent N", usually filtered to one
            # flight. Without these, every admin page load scans the table.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_debug_at "
                         "ON debug_events(id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_debug_subject "
                         "ON debug_events(subject, id DESC)")
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def log(event: str, subject: Optional[str] = None, **detail: Any) -> None:
    """Record one decision.

    `event` is the kind ("closure.decided"). `subject` is what it happened
    to, normally a flight id, so one leg's whole story can be pulled out.
    Everything else becomes JSON detail.
    """
    if not ENABLED:
        return
    global _counter
    try:
        payload = json.dumps(_scrub(detail), default=str)[:4000]
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO debug_events (at, event, subject, detail) "
                "VALUES (?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), event,
                 str(subject) if subject else None, payload),
            )
            conn.commit()
            with _lock:
                _counter += 1
                due = _counter >= _TRIM_EVERY
                if due:
                    _counter = 0
            if due:
                conn.execute(
                    "DELETE FROM debug_events WHERE id <= "
                    "(SELECT MAX(id) FROM debug_events) - ?", (MAX_EVENTS,))
                conn.commit()
        finally:
            conn.close()
    except Exception:
        # Deliberately silent. See the module docstring: a poll must never
        # fail because a diagnostic row could not be written.
        pass


def recent(limit: int = 100, subject: Optional[str] = None,
           event: Optional[str] = None, q: Optional[str] = None,
           after_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Most recent events first. Returns [] rather than raising.

    `subject` is now a CONTAINS match, not an equality one (1.8.0). It held
    a full flight id — "2026-08-04-3729-DFW-OKC" — and the filter box asked
    you to type all of it exactly right to see anything, which meant the
    filter went unused. Typing "3729" now works.

    `q` searches subject, event and the raw detail JSON together, so a tail
    number or a threshold value finds the lines that mention it without
    needing to know which column it lives in.

    `after_id` returns only rows NEWER than an id, which is what the live
    tail polls with — it asks for what it has not seen rather than
    re-fetching the last 100 lines every two seconds.
    """
    try:
        conn = get_connection()
        try:
            sql = "SELECT id, at, event, subject, detail FROM debug_events"
            where, args = [], []
            if subject:
                where.append("subject LIKE ?")
                args.append(f"%{subject}%")
            if event:
                where.append("event LIKE ?")
                args.append(f"{event}%")
            if q:
                where.append("(subject LIKE ? OR event LIKE ? OR detail LIKE ?)")
                args += [f"%{q}%"] * 3
            if after_id:
                where.append("id > ?")
                args.append(int(after_id))
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(int(limit))
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            try:
                detail = json.loads(r["detail"]) if r["detail"] else {}
            except Exception:
                detail = {"raw": r["detail"]}
            out.append({"id": r["id"], "at": r["at"], "event": r["event"],
                        "subject": r["subject"], "detail": detail})
        return out
    except Exception:
        return []


def event_names(limit: int = 40) -> List[str]:
    """Distinct event names, most recent first. Populates the filter menu.

    Typing an event prefix meant knowing the names — `closure.inputs`,
    `enrichment.spend` — which are only discoverable by reading the source.
    A menu built from what is actually in the log needs no such knowledge.
    """
    try:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT event, MAX(id) AS last FROM debug_events "
                "GROUP BY event ORDER BY last DESC LIMIT ?", (int(limit),)
            ).fetchall()
        finally:
            conn.close()
        return [r["event"] for r in rows]
    except Exception:
        return []


def clear() -> int:
    """Empty the log. Returns how many rows went, or -1 on failure."""
    try:
        conn = get_connection()
        try:
            n = conn.execute("SELECT COUNT(*) AS c FROM debug_events").fetchone()["c"]
            conn.execute("DELETE FROM debug_events")
            conn.commit()
            return int(n)
        finally:
            conn.close()
    except Exception:
        return -1
