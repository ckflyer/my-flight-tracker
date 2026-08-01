"""Aircraft metadata: auto-populated from OpenSky's public aircraft database
(registration/manufacturer/model/typecode by icao24) the first time we see a
tail, with zero manual entry. OpenSky's live states endpoint doesn't include
this info at all — only their separate bulk CSV export does — so the first
sighting of a new icao24 kicks off a background lookup against that CSV and
caches the result locally. Until that finishes (or if the aircraft isn't in
OpenSky's database at all), we just don't have extra info to show yet."""
from __future__ import annotations

import csv
import io
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

from .db import get_connection

OPENSKY_AIRCRAFT_DB_URL = "https://opensky-network.org/datasets/metadata/aircraftDatabase.csv"
RETRY_AFTER_DAYS = 14  # don't hammer a lookup that came back empty; retry occasionally in case the DB gets updated
_lookup_in_progress: set = set()
_lookup_lock = threading.Lock()


def note_aircraft_seen(icao24: Optional[str]) -> None:
    """Upsert a bare row the first time we see an icao24 via live tracking,
    bump last_seen on subsequent sightings, and kick off a background
    metadata lookup if we don't have one yet (or it's worth retrying)."""
    if not icao24:
        return
    icao24 = icao24.strip().lower()
    if not icao24:
        return
    now = datetime.utcnow().isoformat() + "Z"
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO aircraft (icao24, first_seen, last_seen)
            VALUES (?, ?, ?)
            ON CONFLICT(icao24) DO UPDATE SET last_seen = excluded.last_seen
            """,
            (icao24, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT model, typecode, lookup_attempted_at FROM aircraft WHERE icao24 = ?",
            (icao24,),
        ).fetchone()
    finally:
        conn.close()

    have_info = row and (row["model"] or row["typecode"])
    if have_info:
        return

    should_retry = True
    if row and row["lookup_attempted_at"]:
        try:
            last_try = datetime.fromisoformat(row["lookup_attempted_at"])
            should_retry = datetime.utcnow() - last_try > timedelta(days=RETRY_AFTER_DAYS)
        except Exception:
            should_retry = True

    if not should_retry:
        return

    with _lookup_lock:
        if icao24 in _lookup_in_progress:
            return
        _lookup_in_progress.add(icao24)

    thread = threading.Thread(target=_background_lookup, args=(icao24,), daemon=True)
    thread.start()


def _background_lookup(icao24: str) -> None:
    try:
        result = _lookup_from_opensky_db(icao24)
        _save_lookup_result(icao24, result)
    finally:
        with _lookup_lock:
            _lookup_in_progress.discard(icao24)


def _lookup_from_opensky_db(icao24: str) -> Optional[Dict[str, str]]:
    """Stream OpenSky's public aircraft database CSV looking for this
    icao24. Stops as soon as it's found rather than downloading/parsing the
    whole ~50-100MB file every time. Returns None on any failure (offline,
    timeout, not found) — callers just treat that as "no info available"."""
    try:
        with requests.get(OPENSKY_AIRCRAFT_DB_URL, stream=True, timeout=60) as r:
            r.raise_for_status()
            reader = csv.reader(io.TextIOWrapper(r.raw, encoding="utf-8", errors="replace"))
            header = None
            for row in reader:
                if header is None:
                    header = [h.strip().strip('"').lower() for h in row]
                    continue
                if not row:
                    continue
                row_icao24 = row[0].strip().strip('"').lower()
                if row_icao24 != icao24:
                    continue
                d = dict(zip(header, [c.strip().strip('"') for c in row]))
                return {
                    "registration": d.get("registration") or "",
                    "manufacturer": d.get("manufacturername") or "",
                    "model": d.get("model") or "",
                    "typecode": d.get("typecode") or "",
                }
    except Exception as e:
        print(f"[aircraft] opensky db lookup error: {e}")
        return None
    return None


def _save_lookup_result(icao24: str, result: Optional[Dict[str, str]]) -> None:
    now = datetime.utcnow().isoformat() + "Z"
    conn = get_connection()
    try:
        if result:
            conn.execute(
                """
                UPDATE aircraft SET
                    registration = ?, manufacturer = ?, model = ?, typecode = ?,
                    lookup_attempted_at = ?
                WHERE icao24 = ?
                """,
                (
                    result.get("registration") or None,
                    result.get("manufacturer") or None,
                    result.get("model") or None,
                    result.get("typecode") or None,
                    now,
                    icao24,
                ),
            )
        else:
            conn.execute(
                "UPDATE aircraft SET lookup_attempted_at = ? WHERE icao24 = ?",
                (now, icao24),
            )
        conn.commit()
    finally:
        conn.close()


def get_aircraft_info(icao24: Optional[str]) -> Optional[Dict[str, Any]]:
    if not icao24:
        return None
    icao24 = icao24.strip().lower()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM aircraft WHERE icao24 = ?", (icao24,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = dict(row)
    # A single display-friendly line, e.g. "Embraer ERJ 170-200 STD" or
    # falling back to just the typecode ("E75L") if that's all we have.
    manufacturer = (d.get("manufacturer") or "").strip()
    model = (d.get("model") or "").strip()
    typecode = (d.get("typecode") or "").strip()
    if manufacturer or model:
        d["display_type"] = " ".join(p for p in [manufacturer, model] if p)
    elif typecode:
        d["display_type"] = typecode
    else:
        d["display_type"] = None
    return d


def list_aircraft() -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM aircraft ORDER BY last_seen DESC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
