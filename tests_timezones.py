"""Timezones, DST edges, and arrival-date resolution.

Every bug in here was SILENT. Nothing raised, nothing logged, no leg
disappeared from the page — the times were simply an hour or a day wrong,
and the only way to notice was for a human to look at a schedule and say
"that's not right". Which is what happened, more than once, before this
suite existed.

That silence is the reason these assertions are worth their runtime. A
timezone bug does not announce itself; it just quietly puts the wrong
number in front of a family member, and soon in a logbook.

Run: python tests_timezones.py
"""
import glob
import os
import re
import sys
import tempfile
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

os.environ["PT_DB_FILE"] = os.path.join(tempfile.mkdtemp(), "tz_test.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.models import AirportInfo, FlightLeg              # noqa: E402
from app.view import zone_label, short_zone                 # noqa: E402
from app.timezones import (                                # noqa: E402
    local_to_utc, parse_iso_utc, resolve_arrival_utc, utc_to_local,
)

PASS, FAIL = [], []
CHI = "America/Chicago"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (f"  [{detail}]" if detail and not cond else ""))


def leg(dep_t, arr_t, otz, dtz, d=date(2026, 6, 15)):
    l = FlightLeg(id="t", date=d, flight_number="1", origin="AAA",
                  destination="BBB", dep_time_local=dep_t, arr_time_local=arr_t)
    l.origin_info = AirportInfo(iata="AAA", timezone=otz)
    l.dest_info = AirportInfo(iata="BBB", timezone=dtz)
    return l


def main():
    # -- ordinary conversion ----------------------------------------------
    print("\nOrdinary conversion")
    r = local_to_utc(date(2026, 6, 15), time(7, 15), CHI)
    check("0715 CDT is 1215Z", r.hour == 12 and r.minute == 15, str(r))
    r = local_to_utc(date(2026, 1, 15), time(7, 15), CHI)
    check("0715 CST is 1315Z (winter offset differs)",
          r.hour == 13 and r.minute == 15, str(r))
    check("an unknown zone returns None rather than raising",
          local_to_utc(date(2026, 6, 15), time(7, 15), "Not/AZone") is None)
    check("an empty zone returns None",
          local_to_utc(date(2026, 6, 15), time(7, 15), "") is None)

    # -- DST spring forward: the hour that does not exist ------------------
    print("\nDST spring forward (2026-03-08, clocks jump 0200 -> 0300)")
    r = local_to_utc(date(2026, 3, 8), time(2, 30), CHI)
    check("a nonexistent 0230 still resolves", r is not None)
    back = r.astimezone(ZoneInfo(CHI))
    # 0330 is right: the printed time falls in the skipped hour, so the event
    # happens after the jump. 0130 would be EARLIER than the printed time --
    # the one direction that makes a crew member miss a report. An earlier
    # version of local_to_utc did exactly that.
    check("0230 resolves forward to 0330, not back to 0130",
          back.hour == 3 and back.minute == 30, str(back))
    r = local_to_utc(date(2026, 3, 8), time(1, 30), CHI)
    check("0130 before the jump is untouched",
          r.astimezone(ZoneInfo(CHI)).hour == 1, str(r))
    r = local_to_utc(date(2026, 3, 8), time(4, 0), CHI)
    check("0400 after the jump is untouched",
          r.astimezone(ZoneInfo(CHI)).hour == 4, str(r))

    # -- DST fall back: the hour that happens twice ------------------------
    print("\nDST fall back (2026-11-01, clocks repeat 0100-0159)")
    r = local_to_utc(date(2026, 11, 1), time(1, 30), CHI)
    check("an ambiguous 0130 resolves", r is not None)
    # Two real instants match. The first is what a published schedule means.
    check("0130 takes the FIRST occurrence (0630Z, not 0730Z)",
          r.hour == 6 and r.minute == 30, str(r))
    r = local_to_utc(date(2026, 11, 1), time(3, 0), CHI)
    check("0300 after the repeat is unambiguous",
          r.astimezone(ZoneInfo(CHI)).hour == 3, str(r))

    # -- arrival resolution ------------------------------------------------
    print("\nArrival date resolution (no clock-comparison heuristic)")
    l = leg(time(7, 15), time(9, 12), CHI, CHI)
    check("same-day domestic leg blocks under 3h",
          l.block_time() == timedelta(hours=1, minutes=57), str(l.block_time()))

    l = leg(time(23, 30), time(1, 45), CHI, CHI)
    check("an overnight leg lands the NEXT day",
          l.block_time() == timedelta(hours=2, minutes=15), str(l.block_time()))

    # The case the old heuristic got wrong. Arrival clock (1700) reads LATER
    # than departure (1400), so "arr < dep" was false and no day was added --
    # losing 24 hours on a leg that plainly crosses the date line.
    l = leg(time(14, 0), time(17, 0), "America/Anchorage", "Asia/Tokyo")
    check("date-line westbound gains a day",
          l.block_time() == timedelta(hours=10), str(l.block_time()))

    l = leg(time(8, 0), time(11, 0), "America/Los_Angeles", "Pacific/Honolulu")
    check("westbound LAX-HNL blocks 6h", l.block_time() == timedelta(hours=6),
          str(l.block_time()))
    l = leg(time(22, 0), time(6, 0), "Pacific/Honolulu", "America/Los_Angeles")
    check("eastbound red-eye HNL-LAX blocks 5h",
          l.block_time() == timedelta(hours=5), str(l.block_time()))

    print("\nArrival resolution refuses nonsense")
    dep = local_to_utc(date(2026, 6, 15), time(7, 0), CHI)
    check("an arrival cannot precede its departure",
          resolve_arrival_utc(dep, date(2026, 6, 15), time(7, 0), CHI) is None)
    # An arrival one minute BEFORE departure implies a 23h59m block on the
    # next candidate day. That is a typo, not a leg. The 20h ceiling rejects
    # it and the arrival stays unresolved -- which shows as a missing time
    # the pilot can correct, rather than a confident, wrong, day-long block
    # that would end up in a logbook. Refusing to answer is the right
    # failure here.
    got = resolve_arrival_utc(dep, date(2026, 6, 15), time(6, 59), CHI)
    check("an implausible 24h block is refused, not invented", got is None, str(got))
    # 19h is under the ceiling and does resolve, so the ceiling is a ceiling
    # and not an accidental ban on long legs.
    got = resolve_arrival_utc(dep, date(2026, 6, 15), time(2, 0), CHI)
    check("a genuinely long leg still resolves",
          got is not None and (got - dep) == timedelta(hours=19), str(got))
    check("no destination zone yields None",
          resolve_arrival_utc(dep, date(2026, 6, 15), time(9, 0), "") is None)

    print("\nMissing airport data degrades quietly")
    l = FlightLeg(id="t", date=date(2026, 6, 15), flight_number="1",
                  origin="AAA", destination="BBB",
                  dep_time_local=time(7, 15), arr_time_local=time(9, 12))
    check("no origin info -> no departure instant", l.dep_datetime_utc() is None)
    check("no dest info -> no arrival instant", l.arr_datetime_utc() is None)
    check("no block time either", l.block_time() is None)

    # -- stored timestamps read back ---------------------------------------
    print("\nReading stored timestamps")
    check("a Z suffix parses", parse_iso_utc("2026-06-15T12:15:00Z").hour == 12)
    check("an explicit offset parses and normalises",
          parse_iso_utc("2026-06-15T07:15:00-05:00").hour == 12)
    # Writers in this codebase all intend UTC; a bare value must not be read
    # as machine-local, or every timestamp shifts by the server's offset.
    check("a naive value is assumed UTC",
          parse_iso_utc("2026-06-15T12:15:00").hour == 12)
    check("garbage returns None instead of raising",
          parse_iso_utc("not a time") is None)
    check("None returns None", parse_iso_utc(None) is None)

    print("\nUTC back to local")
    u = local_to_utc(date(2026, 6, 15), time(12, 0), "UTC")
    check("1200Z is 0700 in Chicago", utc_to_local(u, CHI).hour == 7)
    check("a bad zone returns None", utc_to_local(u, "Not/AZone") is None)
    check("None in, None out", utc_to_local(None, CHI) is None)

    # -- the invariant -----------------------------------------------------
    print("\nInvariant: nobody builds instants outside this module")
    offenders = []
    for path in glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "app", "*.py")):
        if os.path.basename(path) == "timezones.py":
            continue
        for n, line in enumerate(open(path), 1):
            m = re.search(r"datetime\.combine\([^)]*tzinfo\s*=\s*([\w.]+)", line)
            # tzinfo=timezone.utc is not a local-to-UTC conversion -- there is
            # no wall clock and no DST to get wrong. The invariant is about
            # NAMED ZONES, which is where the silent bugs live.
            if m and m.group(1) not in ("timezone.utc", "UTC", "utc"):
                offenders.append(f"{os.path.basename(path)}:{n}")
    check("no module hand-rolls a local-to-UTC conversion",
          not offenders, ", ".join(offenders))

    # -- zone LABELS: the display side ------------------------------------
    print("\nZone labels come from one function")
    check("North American zones collapse to two letters",
          zone_label(CHI, date(2026, 7, 15)) == "CT", zone_label(CHI, date(2026, 7, 15)))
    check("the same zone in winter gives the same label",
          zone_label(CHI, date(2026, 1, 15)) == "CT", zone_label(CHI, date(2026, 1, 15)))
    # main.fmt_local used a hard-coded 2026-07-01 sample, so every label it
    # produced was the SUMMER one. Invisible in North America because CDT and
    # CST both collapse to CT; plainly wrong anywhere else.
    check("a winter date outside North America is not labelled summer",
          zone_label("Europe/London", date(2026, 1, 15)) == "GMT",
          zone_label("Europe/London", date(2026, 1, 15)))
    check("a summer date outside North America is",
          zone_label("Europe/London", date(2026, 7, 15)) == "BST",
          zone_label("Europe/London", date(2026, 7, 15)))
    check("Arizona reads MT despite skipping daylight time",
          zone_label("America/Phoenix", date(2026, 7, 15)) == "MT")
    check("a zone with no NA equivalent keeps its own abbreviation",
          zone_label("Asia/Tokyo", date(2026, 7, 15)) == "JST")
    # The old fallback rendered tz_name.split('/')[-1], putting a CITY name
    # where a two-letter zone belonged. That is most of why label lengths
    # looked random on screen.
    check("a bad zone yields None, never a city name",
          zone_label("Not/AZone", date(2026, 7, 15)) is None)
    check("no zone yields None", zone_label(None) is None)
    check("a datetime works as well as a date",
          zone_label(CHI, local_to_utc(date(2026, 7, 15), time(12, 0), CHI)) == "CT")
    check("omitting the date still returns a label", zone_label(CHI) in ("CT",))

    print("\nEvery North American zone collapses to two letters")
    # Generated by labelling every airport in the realistic network and
    # listing what failed to collapse. Newfoundland was the only miss.
    for tz, want in [("America/Chicago", "CT"), ("America/New_York", "ET"),
                     ("America/Denver", "MT"), ("America/Los_Angeles", "PT"),
                     ("America/Anchorage", "AKT"), ("Pacific/Honolulu", "HT"),
                     ("America/Halifax", "AT"), ("America/St_Johns", "NT"),
                     ("America/Phoenix", "MT"), ("America/Puerto_Rico", "AT")]:
        for d in (date(2026, 1, 15), date(2026, 7, 15)):
            got = zone_label(tz, d)
            check(f"{tz.split('/')[-1]} on {d:%b} reads {want}", got == want, got)

    print("\nThe zone is a superscript, not a full-size word")
    css = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "static", "app.css")).read()
    # The superscript is WHY every time can carry a label. At full size a
    # zone beside every time wrapped rows on a phone, which is what drove
    # the old suppress-when-same rule in the first place.
    check("the label is styled small", ".tz {" in css and "0.5625rem" in css)
    # vertical-align:super grows the line box and spaces the rows out; a
    # transform lifts the text without touching layout.
    # Inspect the .tz RULE, not the whole file -- the explanation above the
    # rule names vertical-align in prose, and a naive substring search on
    # the file matches the comment rather than a declaration.
    tz_rule = css.split(".tz {", 1)[1].split("}", 1)[0]
    check("it is lifted by transform, not vertical-align",
          "translateY" in tz_rule and "vertical-align" not in tz_rule)
    check("aria-hidden is not attempted as a CSS property",
          "aria-hidden:" not in css)
    viewer = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "templates", "viewer.html")).read()
    check("the label is hidden from screen readers in markup",
          '<span class="tz">' not in viewer
          and 'class="tz" aria-hidden="true"' in viewer)

    print("\nNo module hand-rolls a zone label")
    offenders = []
    for path in glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "app", "*.py")):
        if os.path.basename(path) == "view.py":
            continue
        for n, line in enumerate(open(path), 1):
            if "tzname()" in line:
                offenders.append(f"{os.path.basename(path)}:{n}")
    check("tzname() is only called inside view.zone_label",
          not offenders, ", ".join(offenders))
    # A hard-coded sample date is the specific mistake that produced summer
    # labels year-round. Nothing should reintroduce one.
    hardcoded = []
    for path in glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "app", "*.py")):
        for n, line in enumerate(open(path), 1):
            if re.search(r"datetime\(20\d\d,\s*\d+,\s*\d+.*tzinfo", line):
                hardcoded.append(f"{os.path.basename(path)}:{n}")
    check("no hard-coded sample date is used to derive a zone",
          not hardcoded, ", ".join(hardcoded))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
