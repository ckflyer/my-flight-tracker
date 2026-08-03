from functools import lru_cache
from typing import Optional, Dict
import airportsdata
from .models import AirportInfo


@lru_cache(maxsize=1)
def _load_airports() -> Dict[str, dict]:
    """Load IATA-keyed airport data once."""
    return airportsdata.load("IATA")


def get_airport(iata: str) -> Optional[AirportInfo]:
    iata = iata.upper().strip()
    data = _load_airports().get(iata)
    if not data:
        return None
    return AirportInfo(
        iata=iata,
        icao=data.get("icao"),
        name=data.get("name"),
        city=data.get("city"),
        country=data.get("country"),
        timezone=data.get("tz") or "UTC",
        lat=data.get("lat"),
        lon=data.get("lon"),
    )


def enrich_leg(leg):
    """Attach origin/dest AirportInfo to a FlightLeg (mutates in place)."""
    leg.origin_info = get_airport(leg.origin)
    leg.dest_info = get_airport(leg.destination)
    return leg
