from datetime import datetime, date, time, timedelta
from typing import Optional, List
from pydantic import BaseModel, Field
from zoneinfo import ZoneInfo


class AirportInfo(BaseModel):
    iata: str
    icao: Optional[str] = None
    name: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    timezone: str  # IANA tz, e.g. America/Chicago
    lat: Optional[float] = None
    lon: Optional[float] = None


class FlightLeg(BaseModel):
    id: str  # unique key, e.g. "2026-06-26-3729-DFW-OKC"
    date: date
    flight_number: str  # e.g. "3729"
    origin: str  # IATA
    destination: str  # IATA
    dep_time_local: time  # local to origin
    arr_time_local: time  # local to destination
    is_deadhead: bool = False
    # Which carrier actually operates this flight. An FFDO line only gives
    # a bare number, and a deadhead is very often on mainline AA or another
    # wholly-owned regional, so assuming "ENY" would look up a flight that
    # doesn't exist. Resolved from flight number + route once, then stored.
    operator_callsign: Optional[str] = None
    trip_start: bool = False  # True if a blank line in the pasted FFDO preceded this leg (new trip)
    # resolved later
    origin_info: Optional[AirportInfo] = None
    dest_info: Optional[AirportInfo] = None

    @property
    def callsign(self) -> str:
        """The callsign this flight actually broadcasts.

        Defaults to Envoy, which is right for the pilot's own legs, but a
        resolved operator wins — a deadhead on AAL or PSA broadcasts its
        own carrier's callsign, not ENY.
        """
        if self.operator_callsign:
            return self.operator_callsign
        return f"ENY{self.flight_number}"

    def dep_datetime_utc(self) -> Optional[datetime]:
        if not self.origin_info:
            return None
        tz = ZoneInfo(self.origin_info.timezone)
        local_dt = datetime.combine(self.date, self.dep_time_local, tzinfo=tz)
        return local_dt.astimezone(ZoneInfo("UTC"))

    def arr_datetime_utc(self) -> Optional[datetime]:
        if not self.dest_info:
            return None
        tz = ZoneInfo(self.dest_info.timezone)
        # Handle overnight / next-day arrival
        arr_date = self.date
        if self.arr_time_local < self.dep_time_local:
            # crossed midnight relative to dep time (simple heuristic)
            arr_date = self.date + timedelta(days=1)
        # More robust: if arrival local hour is much earlier and same calendar day assumed wrong
        local_dt = datetime.combine(arr_date, self.arr_time_local, tzinfo=tz)
        return local_dt.astimezone(ZoneInfo("UTC"))

    def status_at(self, now_utc: datetime) -> str:
        """Return Scheduled / Departed / In Air / Arrived based on schedule times."""
        dep = self.dep_datetime_utc()
        arr = self.arr_datetime_utc()
        if not dep or not arr:
            return "Unknown"
        # small buffers
        if now_utc < dep - timedelta(minutes=15):
            return "Scheduled"
        if now_utc < dep + timedelta(minutes=10):
            return "Departing"
        if now_utc < arr:
            return "In Air"
        return "Arrived"


class Schedule(BaseModel):
    legs: List[FlightLeg] = Field(default_factory=list)
    updated_at: Optional[datetime] = None


class CurrentFlightInfo(BaseModel):
    current: Optional[FlightLeg] = None
    next: Optional[FlightLeg] = None
    past: List[FlightLeg] = Field(default_factory=list)
    upcoming: List[FlightLeg] = Field(default_factory=list)
    all_legs: List[FlightLeg] = Field(default_factory=list)
