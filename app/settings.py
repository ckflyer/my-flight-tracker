"""Per-user app settings, stored on the users table (not a shared JSON file
— that was a single-tenant assumption that doesn't hold once each pilot has
their own OpenSky credentials and preferences)."""
from __future__ import annotations

import os
from pydantic import BaseModel

from .db import get_connection


class AppSettings(BaseModel):
    opensky_client_id: str = ""
    opensky_client_secret: str = ""
    time_format: str = "24"
    show_flightaware: bool = True
    show_fr24: bool = True
    theme: str = "dark"
    poll_seconds: int = 45


def load_settings(user_id: int) -> AppSettings:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT opensky_client_id, opensky_client_secret, time_format,
                   theme, poll_seconds, show_flightaware, show_fr24
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return AppSettings()
    return AppSettings(
        opensky_client_id=row["opensky_client_id"] or "",
        opensky_client_secret=row["opensky_client_secret"] or "",
        time_format=row["time_format"] or "24",
        theme=row["theme"] or "dark",
        poll_seconds=row["poll_seconds"] or 45,
        show_flightaware=bool(row["show_flightaware"]),
        show_fr24=bool(row["show_fr24"]),
    )


def save_settings(user_id: int, s: AppSettings) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE users SET
                opensky_client_id = ?, opensky_client_secret = ?, time_format = ?,
                theme = ?, poll_seconds = ?, show_flightaware = ?, show_fr24 = ?
            WHERE id = ?
            """,
            (
                s.opensky_client_id, s.opensky_client_secret, s.time_format,
                s.theme, s.poll_seconds, int(s.show_flightaware), int(s.show_fr24),
                user_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def apply_opensky_env(s: AppSettings) -> None:
    """Push this user's credentials into process env so the opensky module
    picks them up for this request.

    Known limitation: this is process-wide, so if multiple pilots' requests
    ever land concurrently with different OpenSky credentials, the last one
    to call this wins for that instant. Harmless with today's single active
    pilot; worth revisiting (passing credentials through explicitly instead
    of env vars) if/when concurrent multi-pilot polling becomes real.
    """
    if s.opensky_client_id:
        os.environ["OPENSKY_CLIENT_ID"] = s.opensky_client_id
    if s.opensky_client_secret:
        os.environ["OPENSKY_CLIENT_SECRET"] = s.opensky_client_secret
