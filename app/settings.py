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
            SELECT time_format, theme, poll_seconds, show_flightaware, show_fr24
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return AppSettings()
    return AppSettings(
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
                time_format = ?, theme = ?, poll_seconds = ?,
                show_flightaware = ?, show_fr24 = ?
            WHERE id = ?
            """,
            (
                s.time_format, s.theme, s.poll_seconds,
                int(s.show_flightaware), int(s.show_fr24),
                user_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
