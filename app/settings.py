"""Per-user app settings, stored on the users table (not a shared JSON file
— that was a single-tenant assumption that doesn't hold once each pilot has
their own preferences).

The opensky_client_id / opensky_client_secret columns still exist on the
users table but are no longer read or written: the live data source needs
no credentials. They're left in place because migrations here are
append-only, and dropping columns in SQLite would mean another table
rebuild for no benefit."""
from __future__ import annotations

from pydantic import BaseModel

from .db import get_connection


class AppSettings(BaseModel):
    aeroapi_enabled: bool = False
    aeroapi_key: str = ""
    # The pilot's own monthly ceiling, in dollars. Replaces the old
    # allow-overage toggle: rather than "stop at our number, or don't stop
    # at all", the pilot sets the number and it is always enforced. 0 is
    # allowed and means "never query" — a deliberate off switch that keeps
    # the key stored.
    aeroapi_budget: float = 4.90
    time_format: str = "24"
    show_flightaware: bool = True
    show_fr24: bool = True
    theme: str = "dark"
    poll_seconds: int = 15


def load_settings(user_id: int) -> AppSettings:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT aeroapi_enabled, aeroapi_key, aeroapi_budget, time_format, theme, poll_seconds,
                   show_flightaware, show_fr24
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return AppSettings()
    return AppSettings(
        aeroapi_enabled=bool(row["aeroapi_enabled"]),
        aeroapi_key=row["aeroapi_key"] or "",
        aeroapi_budget=(float(row["aeroapi_budget"])
                        if row["aeroapi_budget"] is not None else 4.90),
        time_format=row["time_format"] or "24",
        theme=row["theme"] or "dark",
        poll_seconds=row["poll_seconds"] or 15,
        show_flightaware=bool(row["show_flightaware"]),
        show_fr24=bool(row["show_fr24"]),
    )


def save_settings(user_id: int, s: AppSettings) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE users SET
                aeroapi_enabled = ?, aeroapi_key = ?, aeroapi_budget = ?,
                time_format = ?, theme = ?, poll_seconds = ?,
                show_flightaware = ?, show_fr24 = ?
            WHERE id = ?
            """,
            (
                int(s.aeroapi_enabled), s.aeroapi_key, float(s.aeroapi_budget),
                s.time_format, s.theme, s.poll_seconds,
                int(s.show_flightaware), int(s.show_fr24),
                user_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
