from datetime import datetime, date, time, timedelta
from typing import Optional, List
from pydantic import BaseModel, Field
from zoneinfo import ZoneInfo

from .carriers import home_callsign
from .timezones import local_to_utc, resolve_arrival_utc


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
    # Which carrier actually operates this flight. A bid line only gives a
    # bare number, and a deadhead is very often on the mainline or a sibling
    # regional, so assuming the home prefix would look up a flight that
    # doesn't exist. Resolved from flight number + route once, then stored.
    operator_callsign: Optional[str] = None
    trip_start: bool = False  # True if a blank line in the pasted FFDO preceded this leg (new trip)
    # resolved later
    origin_info: Optional[AirportInfo] = None
    dest_info: Optional[AirportInfo] = None

    @property
    def callsign(self) -> str:
        """The callsign this flight actually broadcasts.

        Defaults to the home carrier (carriers.HOME_PREFIX), which is right
        for the pilot's own legs, but a resolved operator always wins — a
        deadhead broadcasts its own carrier's callsign, not the home one.
        """
        if self.operator_callsign:
            return self.operator_callsign
        return home_callsign(self.flight_number)

    def dep_datetime_utc(self) -> Optional[datetime]:
        """Departure as a real instant. See app/timezones.py for the DST rules."""
        if not self.origin_info:
            return None
        return local_to_utc(self.date, self.dep_time_local, self.origin_info.timezone)

    def arr_datetime_utc(self) -> Optional[datetime]:
        """Arrival as a real instant.

        A bid line prints an arrival clock time with no date. Until v1.1.0
        the date was guessed with `if arr_time_local < dep_time_local: add a
        day`, which compares a clock in the ORIGIN's zone against a clock in
        the DESTINATION's zone as though they shared one. It happened to be
        right for most domestic legs and was wrong outright once the offsets
        differed enough — an ANC-NRT leg lost a whole day.

        Now the arrival date is RESOLVED, not inferred: try each candidate
        date and keep the first instant that lands after departure inside a
        believable block. Correct for every zone pair, no special cases.
        """
        if not self.dest_info:
            return None
        dep = self.dep_datetime_utc()
        if dep is None:
            return None
        return resolve_arrival_utc(dep, self.date, self.arr_time_local,
                                   self.dest_info.timezone)

    def block_time(self) -> Optional[timedelta]:
        """Scheduled block, OUT to IN. None if either end is unresolved."""
        dep, arr = self.dep_datetime_utc(), self.arr_datetime_utc()
        return (arr - dep) if (dep and arr) else None

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
