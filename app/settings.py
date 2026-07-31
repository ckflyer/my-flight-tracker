"""Persistent app settings stored in data/settings.json."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from pydantic import BaseModel, Field

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "settings.json"

DEFAULTS: Dict[str, Any] = {
    "opensky_client_id": "",
    "opensky_client_secret": "",
    "time_format": "24",          # "12" or "24"
    "show_flightaware": True,
    "show_fr24": True,
    "theme": "dark",              # "dark" or "light"
    "poll_seconds": 45,
}


class AppSettings(BaseModel):
    opensky_client_id: str = ""
    opensky_client_secret: str = ""
    time_format: str = "24"
    show_flightaware: bool = True
    show_fr24: bool = True
    theme: str = "dark"
    poll_seconds: int = 45


def load_settings() -> AppSettings:
    if not DATA_FILE.exists():
        return AppSettings()
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return AppSettings(**{**DEFAULTS, **data})
    except Exception:
        return AppSettings()


def save_settings(s: AppSettings) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(s.model_dump(), indent=2),
        encoding="utf-8",
    )


def apply_opensky_env(s: AppSettings | None = None) -> None:
    """Push stored credentials into process env so opensky module picks them up."""
    import os
    s = s or load_settings()
    if s.opensky_client_id:
        os.environ["OPENSKY_CLIENT_ID"] = s.opensky_client_id
    if s.opensky_client_secret:
        os.environ["OPENSKY_CLIENT_SECRET"] = s.opensky_client_secret
