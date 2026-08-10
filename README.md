# Pilot Tracker

Personal flight schedule + live tracking app for airline pilots (built around
Envoy / American Eagle's FFDO schedule format) and the people waiting on
them. Think "Flighty, but for crew and family" — no social features, no
stats/gamification, just the tracking.

> **Working on this with an AI assistant?** Read *For the AI assistant*
> below before changing anything, and update this file before packaging a
> new zip. This file is the only thing that carries between sessions.

- Paste your FFDO schedule; automatic current/next/past flights based on
  local block times
- Live ADS-B position via Airplanes.live, with a real flight-phase state machine
- Flown tracks kept per flight for 30 days, so past flights replay their
  actual path on the map
- Nothing ever full-page reloads: live data, flight switching, and the
  active flight changing all update in place
  (Scheduled → Departing → Taxi-out → In Air → Landing → Taxi-in → Arrived) —
  not just a clock guess
- Breadcrumb trail, flight-progress bar, distance-to-go, ETE, and a classic
  green/yellow/red weather radar overlay on the current flight's map
- Aircraft registration/type arrive with the live position — no lookup,
  no manual entry
- One-tap FlightRadar24 / FlightAware links that prefer the installed app on
  Android
- Pilot login (username + password) plus a 5-digit share code so anyone you
  give it to can view your flights without an account
- Installable as a home-screen app on iOS/Android (icon, manifest, etc.)
- Designed to self-host on TrueNAS / Dockge / any Linux box with Docker

## For the AI assistant: read this first, update it last

**This file is the handoff between sessions.** The sandbox is wiped between
chats, so nothing survives except this repo. Whatever isn't written here is
lost. Assume the person you're helping has no background in code and reads
this file to find out what state their own project is in.

### At the start of a session

1. Read this whole section, then **Current state** and **Open items** below.
2. Read the code before changing it. The summary here is a map, not the
   territory, and it can be out of date — the code is the truth.
3. If the packaged zip and the deployed build seem to disagree, say so
   before editing. That has already happened once and cost a working
   feature (see *Restored missing JavaScript*).

### At the end of a session — required

Update this file **before** packaging the zip. Specifically:

- Bump the version in `app/version.py` and add a section under **Version
  history** describing what changed and, more importantly, **why**.
- Update **Current state** and **Open items** so they describe the project
  as it is now, not as it was.
- Record any failure you hit and how it was diagnosed. A bug that gets
  fixed twice is a bug that wasn't written down the first time.

Write for someone non-technical. Explain the reason a thing was done, not
just the change — the reasoning is what stops the next session undoing it.

### Ground rules learned the hard way

- **Never package anything from `data/`.** `data/secret_key.txt` signs
  session cookies; shipping one logs out the pilot and every share-link
  viewer at once. The packaging step must exclude `data/*.db`,
  `data/secret_key.txt` and `data/settings.json`. This has happened.
- **Verify after any multi-part edit.** The recurring failure mode in this
  project is colliding edits in one function producing duplicated lines,
  wrong column names, lost constants, or silently deleted functions. Re-read
  what you changed. Run the tests.
- **The pilot's domain corrections are usually right.** He flies these
  legs. When he says something doesn't match reality, it doesn't — his
  corrections have repeatedly caught real bugs.
- **Times are local to their own airport.** This trips up code and test
  fixtures alike; building a departure time from UTC clock hands puts a PHX
  leg seven hours out.
- Sandbox shell notes: `pkill -f` self-matches the shell, use `pkill -x
  uvicorn`; `cd X && cmd &` backgrounds the whole list.
- Test suites share one database, so ordering matters, and login
  rate-limiting trips after repeated runs (restarting clears it).

### Workflow this project uses

The pilot extracts the zip into his GitHub repo, pushes, then runs
`update.sh` on TrueNAS (Dockge) to `git pull` and restart. So **the zip must
be complete and self-consistent** — it is dropped in wholesale. Don't ship
patch scripts or partial files, and don't assume he can run commands to fix
up a package after extracting it.

## Current state

**Version 5.0 — the data rebuild.** Seven tables became three, the page
stopped writing to the database, and the single status badge became two
pills. See *Version history* for the full list and *How the data is
organised* for why.

Working: schedule import, live ADS-B tracking, flight tracks, AeroAPI
enrichment with a pilot-set monthly spend limit, share codes, calendar, the
two-pill phase/status display, and closure.

Tests: 106 across four suites, all passing.

| Suite | Covers |
| --- | --- |
| `tests_flight_row.py` (43) | write modes, both tag ladders, closure guards, retention |
| `tests_poller_end_to_end.py` (27) | a whole flight, gate to gate, with a scripted ADS-B feed |
| `tests_past_leg_detail.py` (19) | past-leg and T-30 preview rendering |
| `tests_budget_limit.py` (17) | the monthly spend cap at its real enforcement point |

**v5.0 has not been run on the real box yet.** It has only been exercised
in a sandbox against fake data. Take a copy of `data/flighttracker.db`
before the first `update.sh`.

## Open items

- **First-deploy check.** Watch the container log on first boot for the
  three migration lines (`dropped the dead v4 positions table`, `carried N
  track points over`, `carried N schedule legs over`), then confirm the
  schedule and past tracks look right in the app.
- Tune query spend toward the $5 free credit. At ~46 legs/month and ~5
  queries a leg the app currently lands near $1.25 with a worst case around
  $2.00, so there is real headroom.
- `check_aeroapi.py` and `check_live_source.py` need running on the real
  box; a sandbox can't reach either API.
- The `/account/usage` response shape is still unverified against the real
  endpoint. `refresh_usage()` logs an unrecognised shape rather than
  silently reporting zero, so check the container logs for it.
- Distribution is undecided: self-hosted free vs commercial. Airplanes.live
  is non-commercial, and the AeroAPI Personal tier is personal-use only.

## Quick start

```bash
# clone
git clone <your-repo-url>
cd flight-tracker

# install
python3 -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

# run
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000/` — first visit redirects to `/setup` to
create your pilot account (username, password, optional email). After that,
`/login` is where everyone comes in: pilots use username+password, viewers
(family, whoever you share the code with) use the 5-digit tracking code
shown on `/admin`.

## Schedule format

Paste exactly like this (one leg per line):

```
06/26/2026 3729 DFW 1742 OKC 1837
06/26/2026 3729 OKC 1911 DFW 2011
06/26/2026 3566 DFW 2227 ICT 2351
```

`MM/DD/YYYY  FLIGHT  ORIG  DEPTIME  DEST  ARRTIME`

Times are local block times at each airport. Each row has an "×" button on
`/admin` to delete it individually if needed.

## Accounts & sharing

- **Pilots** log in with a username/password created during first-run setup
  at `/setup`. Only a pilot can edit the schedule (`/admin`) or settings
  (`/settings`).
- **Viewers** don't need an account — just the 5-digit code shown at the top
  of `/admin`, entered on the "Viewer access" side of `/login`. Any number of
  people can use the same code at once, and it stays valid indefinitely once
  someone's logged in with it.
- **Regenerating the code** (button on `/admin`) instantly revokes access for
  anyone still using the old one — useful if you want to cut off a specific
  person without affecting anyone else, since you'd just share the new code
  with everyone you still want to have access. There's a **Share** button
  next to it that uses the phone's native share sheet (or copies to
  clipboard) to send the link + code.
- The data model is user-scoped throughout (separate schedules, separate
  separate everything per pilot account) as groundwork
  for supporting more than one pilot on the same install later. Right now
  there's no public signup — accounts are created only via the one-time
  `/setup` bootstrap.

## Project layout

```
app/
  main.py         FastAPI routes and page rendering. READS ONLY.
  poller.py       the background engine. The ONLY thing that decides anything.
  flights.py      the flight row: read, write, merge rules, retention
  tags.py         the two pills — phase ladder and status
  view.py         turns a flight row into what the card shows. READ-ONLY.
  closure.py      when a leg is over, and on whose authority
  flightmatch.py  which physical aircraft is flying this leg
  enrichment.py   when to spend an AeroAPI query, and where the answer goes
  track.py        the breadcrumb trail, plus progress / distance / ETE maths
  livesource.py   shared cache and rate-limit floor over the ADS-B provider
  airplaneslive.py  the ADS-B provider itself
  aeroapi.py      the FlightAware client
  carrier.py      which airline actually operates a deadhead
  db.py           the three-table schema, and the v4 migration
  schedule.py     splitting the schedule into past / current / upcoming
  parser.py       reads a pasted FFDO line
  models.py       FlightLeg and friends
  auth.py         accounts, share codes, sessions
  settings.py     per-user preferences
  airports.py     IATA lookup      geo.py  great-circle distance
  ratelimit.py    login throttling  version.py  the footer version number
templates/        viewer.html (the app), admin, calendar, settings, login
```

## How the data is organised

**Three tables. That is the whole database.**

| Table | What it holds |
| --- | --- |
| `users` | accounts, preferences, AeroAPI key, spend counters |
| `flights` | ONE ROW PER LEG — every fact about that flight, in a named column |
| `positions` | the breadcrumb trail, keyed by flight rather than by user |

Before v5 this was seven tables, and a single leg's story was spread across
four of them: `legs` (the schedule), `flight_aircraft` (what ADS-B had
seen), `flight_enrichment` (a JSON blob of what the airline said) and
`flight_closeout` (another JSON blob). **Nothing owned the flight.** Four
modules each reached into their own table, and `compute_live_payload`
reconciled the pieces at DISPLAY time — on every page render, for every
viewer. That reconciliation is where the ordering bugs lived. Two more
tables, `aircraft` and the old user-scoped `positions`, were completely
dead: nothing had read or written them in several versions.

### ADS-B and airline values are kept SEPARATE

`off_actual_api` and `off_observed` are both "when the wheels came off".
They are deliberately different columns. That means the card can say WHICH
one it is showing, the two can be compared when they disagree, and a
lagging airline record can never silently overwrite something we watched
happen. Merging them into one column would throw away the disagreement,
which is the interesting part.

The four events are all doubled this way:

| Event | Airline's figure | What we observed |
| --- | --- | --- |
| Gate-out | `out_actual_api` | `out_observed` |
| Wheels-off | `off_actual_api` | `off_observed` |
| Wheels-on | `on_actual_api` | `on_observed` |
| Gate-in | `in_actual_api` | `in_observed` |

**Airline wins for display, with one exception.** Gates, delays,
cancellations, diversions and revised times: the airline knows things ADS-B
cannot, so it wins outright. But for the four events above, whichever
source is FURTHER ALONG wins, and neither may move the flight backwards.
The two sources fail in opposite directions — the airline runs late, ADS-B
runs blind — so letting whichever notices first win is what makes it
correct. Blanket airline priority would reintroduce the bug where a flight
reads "In air" while visibly parked, because `actual_on` publishes with a
lag.

### The three write modes

Choosing the right one is most of the correctness of this app. See
`flights.py`.

- **ONCE** — the first value we ever get is kept forever. For things that
  happened at a moment in time: wheels-off, the aircraft hex, the airline's
  originally published schedule. Writing these "latest wins" would let a
  re-query overwrite the truth with a later restatement of it.
- **LATEST** — the new value wins, **but a blank never overwrites a known
  value**. For things that genuinely change: position, revised estimates,
  gate assignment. The blank guard is the whole point. A poll that comes
  back empty because the aircraft is over west Texas with no receiver
  nearby must not erase what we knew a minute ago.
- **ALWAYS** — unconditional overwrite, including with nothing. Only for
  recomputed values like progress and ETE, where "we can't work this out
  right now" is itself the right thing to display.

### One engine, not two

The poller is the only thing that decides anything. It fetches, judges,
records, advances the tags, spends queries and closes legs. The page reads
the row and renders it.

In v4 the page did all of that too — `compute_live_payload` ran on every
render and every browser poll, and it fetched live data, wrote track points
and advanced the aircraft state machine. Two engines ran the same logic on
different clocks, and whichever got there first changed the answer. A
family member opening the app could move the flight's state.

`poll_once(now)` takes the clock as an argument so one sweep runs at
exactly one instant. It used to let `get_current_info` read the wall clock
separately from the preview window, so the two could disagree about which
leg was current.

## The two pills

**Phase** answers "where is the aeroplane right now". **Status** answers
"is the plan holding". They are independent, which is what lets a diversion
show as `Diverted · Taxi-in` — in v4 they shared one slot, so you saw one
or the other but never both.

### Phase — always the theme blue, and ONLY EVER MOVES FORWARD

```
Scheduled → Taxi-out → In air → Landing → Taxi-in → Arrived
```

Forward-only is the fix for the most visible bug in v4. ADS-B coverage
drops somewhere over west Texas, the old phase machine returned "Unknown",
and the card went from "In air" to "Unknown" — so to the person watching,
the app forgot where he was. **Nothing had happened. We had just stopped
hearing.** Losing the signal is a fact about our RECEPTION, not about the
aeroplane, and it now shows as a note beside a phase that stays put:
"no signal for 14 min".

The Unknown phase is gone entirely, and so is the old "Departing" — a
scheduled time passing is not evidence the aircraft moved.

Every branch reads either something the aircraft broadcast or something the
airline published. A leg with no ADS-B coverage at all still gets a phase
from the airline's OOOI, which is exactly when it matters most.

**Only closure produces Arrived.** The phase machine never invents it.
"The aircraft stopped" and "the flight is over" are different claims.

### Status — carries the only colour, and is BLANK when there's nothing to say

There is no "on time" pill. A green badge on every normal flight is
wallpaper and the eye stops seeing it. Colour is how you find trouble, so
it is spent only on trouble — which is also why the phase pill is always
blue and never green, amber or red.

| Status | Fires when |
| --- | --- |
| Cancelled | the airline says so. **Sticks**, and hides the phase pill entirely |
| Diverted | the airline says so. **Sticks** — a flight that diverted diverted |
| Delayed | see below |

Status, unlike phase, **moves both ways**. If the airline pushes departure
to 14:20 and then pulls it back to 13:55, the pill clears. That isn't the
app forgetting; it's a real improvement in his day. Cancelled and Diverted
are the exceptions and never clear.

### Delayed means the AIRLINE PUSHED IT, not that it left late

Two conditions, and both are required:

1. **The airline must have actually moved something** — an estimate (or an
   actual) that differs from the airline's own scheduled time.
2. **The revised time must land later than the FFDO time.** He flies the
   bid line, so that is what late means here.

Condition 1 exists because the airline's published schedule and the bid
line routinely differ by a couple of minutes with nothing wrong. Comparing
the published time straight to the FFDO time would leave the pill
permanently lit. Condition 2 exists because the bid line is the reference
that matters to him.

Before pushback this is measured on departure; after it, on arrival — a
late departure that makes the time up enroute stops showing a pill.

v4's `DELAY_STATUS_MIN` fired off observed lateness, so a 12-minute
pushback lit up "Delayed" when nothing had actually gone wrong. **That is
now impossible.** The lateness NOTE is separate and stays honest
regardless: a flight can read no-pill and still say "out 12 min late".
Those aren't contradictory; they're two true things.

## Matching the right aircraft

**Unchanged in behaviour from v4** — the most correct part of the app, and
it earned that the hard way. Only its storage moved, from the old
`flight_aircraft` table into columns on the flight row.

Live data is looked up by callsign, and a callsign is not unique to a leg.
Regional turns fly out and back under one flight number — 3700 DFW-MFE and
3700 MFE-DFW the same day — and the return departs well inside the window
the outbound stays current for. A plain callsign match locks onto the wrong
aircraft.

**What it deliberately does NOT do:** judge an aircraft by which way it is
pointing. An earlier version rejected aircraft heading away from the
destination, which breaks the moment a flight diverts — a DFW-OKC leg
turning back to DFW looks exactly like the return flight, so the guard
disowned the user's own aeroplane at precisely the moment anyone watching
most needed to see it. Holds, opposite-flow departures and arrival
vectoring break it the same way. Geometry cannot tell "going somewhere
else" apart from "going somewhere else on purpose". There is no route data
to compare against either — ADS-B does not transmit origin or destination.

**What it does instead: identity.** Every aircraft broadcasts a unique
ICAO 24-bit hex address.

0. **ARBITRATE** — when several legs share a callsign that day, only the
   one actually in progress may claim the aircraft: the latest leg whose
   scheduled departure has arrived. Deterministic, and needs no observation
   at all — which matters because release depends on seeing the aircraft
   stopped at the outstation, and small fields often have no coverage.
1. **ACQUIRE** — a leg adopts an aircraft on its callsign when it is at the
   ORIGIN, or during a window around scheduled departure. The window covers
   outstations with no receiver, where a flight may first appear already
   enroute. A turn's return leg can't depart until this one has landed, so
   it never falls in that window.
2. **HOLD** — from then on only that hex is accepted, unconditionally,
   wherever it goes. Diversions, holds and returns to the departure field
   are all followed.
3. **RELEASE** — once the leg closes it accepts nothing further, from any
   source. A closed leg can never adopt the return flight.

The hex is stored as one column, `aircraft_hex`. There is no lookup table
and no learning — the tail number and type now arrive free with the
position. (The old `aircraft` table that did those lookups was dead and has
been dropped.)

## Closing a leg out

One decision, one recorded reason. Once closed a leg is **frozen**: no
polling, no live data, no recomputation, so a past flight's numbers never
drift as late data trickles in.

| `closed_by` | Meaning |
| --- | --- |
| `airline` | the airline's own gate-in, or a cancellation |
| `relaunch` | the aircraft took off AGAIN. Unambiguous, free, no timers |
| `observed` | we watched it land, stop, and go quiet |
| `backstop` | nothing left to learn, and well past arrival |

### Observed arrival now closes the leg, even with an API key

It didn't in v4: only the airline's `actual_in` could close a leg for a
pilot with a key, **which is why closeout hung**. The app would watch the
aeroplane park, then ask FlightAware every ten minutes for an hour and a
half, then wait three hours more. `actual_in` is the OOOI field most often
missing entirely.

By the pilot's own call, a confirmed landing followed by 5 minutes stopped
and 8 minutes silent is good enough to say Arrived. It records as
`observed`, and **a late airline gate-in still upgrades it** to the
airline's figure and time. Nothing is lost.

Both halves are required. A plane waiting off-gate for a stand can sit
stationary for half an hour still transmitting; a signal dropping out over
a ramp with poor coverage means nothing on its own. An aircraft that has
genuinely blocked in stops moving *and* goes dark.

### Two v5 bug fixes, both found by the pilot

**1. The backstop could fire on a delayed flight before it even left.** It
anchored on the best available arrival estimate, but with no airline data
that fell back to the SCHEDULED arrival — so a three-hour delay meant the
backstop clock expired right around the time he actually pushed. Worse, its
"has it gone quiet?" test passed when there had been no signal EVER, which
is exactly the case at an outstation with no receiver. A delayed flight
from a small field could close itself at the gate.

Fixed two ways. The backstop cannot start counting until the flight has
demonstrably BEGUN — ADS-B saw it airborne, or the airline published a
gate-out or wheels-off. Before that there is no clock at all. And with no
revised arrival to anchor on, it anchors on the observed departure plus the
scheduled block time, never on the original timetable.

**2. An observed arrival could be read off an aircraft that never moved.**
Stationary-and-quiet is only meaningful AFTER a landing. Sitting at the
departure gate with the transponder off looks identical. The observed route
now requires wheels-on to be known first, from either source.

## Arrival times: fact before forecast

Arrival is taken in this order: the airline's actual gate-in, then **our
own observed gate stop**, then the estimate. The airline publishes
`actual_in` with a lag, so a flight can be parked while the only airline
figure available is an hour-old forecast. Presenting that forecast as the
arrival time is how "arrived 4:05, 11 minutes early" appeared for a flight
that actually blocked in at 4:11.

When a revised time exists the card shows THAT time, with the scheduled one
struck through beneath it. Delta and displayed time are both derived from
minute-truncated values so they can never disagree by a rounding minute.

Tolerance is zero, by the pilot's call: one minute late IS late, and a card
printing 5:59 beside a crossed-out 5:57 and calling it on time is arguing
with itself.

## Progress cannot run on the clock

Progress is pinned to zero until there is EVIDENCE the aircraft left, and
comes only from a live position. v4's elapsed-time fallback measured the
clock against the schedule, so a flight still at the gate showed "27.7% en
route" and, once past its scheduled arrival, 100%. **No live fix now means
no figure at all and the bar hides.** Better than a number derived from a
timetable everyone already knows is wrong.

Time-to-go comes from live groundspeed and distance where possible, and
otherwise from the airline's REVISED arrival — never the bare schedule. A
flight two hours late does not have five minutes to go just because the
timetable says so.


## When AeroAPI is queried

Deliberately almost all CLOCK-driven. An earlier version triggered on "a
position was stored", which sounds like a state change but is true on
nearly every poll of an airborne aircraft — it burned 8 queries mid-cruise
telling us nothing, and hit the per-leg cap on an ordinary flight.

  * **T-30** — first look: gate, revised arrival, and any delay already
    published. By then the flight plan is filed, so all three exist; an
    hour out they usually don't yet.

    This one needs the background poller to reach the leg BEFORE it becomes
    the current flight. A leg isn't `current` until T-20, so the poller
    carries its own `PREVIEW_WINDOW` (35 min) and sweeps imminent upcoming
    legs too. Without that the T-30 branch is simply unreachable and no
    airline data arrives before pushback — which is exactly what happened
    up to v4.4. The window is deliberately in the poller rather than in
    `get_current_info`, because "current" also drives flight selection, the
    map and the card, and moving that boundary would change all of them.
  * **T+20, then every 30 min** — while the aircraft is still ON THE
    GROUND past its departure time. One prompt check, then a slower watch,
    capped at 3 total. It stops the moment the aircraft is seen airborne,
    handing straight over to the cruise checks rather than waiting for the
    next 30-minute tick.
  * **3 cruise checks** — evenly spaced between ACTUAL departure and
    estimated wheels-on. They require the aircraft to genuinely be off the
    ground: gated only on an anchor, they fired while a delayed flight sat
    at the gate. Any falling inside the 20-minute floor are skipped
    outright (the latest due checkpoint wins), so a short leg uses fewer
    rather than firing them all late.
  * **Wheels down + 5** — often already on stand, closing the leg in one
    query instead of several. Fires once. Needs ADS-B. Touchdown is
    DEBOUNCED: on_ground must hold for 60 seconds at under 90 kts before it
    counts, so a single bad alt_baro frame on approach can't trigger it
    early. The recorded time is backdated to the wheels-down moment, not to
    when confirmation finished.
  * **Closeout** — every 10 minutes until gate-in, capped at 2 attempts.
    After that the observed close or the backstop ends the leg instead.
  * **Arrival fallback** — for legs with NO ADS-B at all, where no
    wheels-down event can ever fire. Capped at 2.

Both repeating triggers are capped because they loop waiting for an answer
that sometimes never comes; uncapped, a missing gate-in ate the whole
per-leg budget. After the caps, the backstop closes the leg instead.

Cruise checks anchor on the airline's wheels-off, or on ADS-B's observed
takeoff when the API hasn't caught up — without that fallback a flight
departing between ground checks has no anchor and cruise checks can't
start at all.

Every trigger has its own cap; none borrows from another. The reachable
maximum is 10 with ADS-B and 9 without, against a hard ceiling of
`MAX_QUERIES_PER_LEG = 10`, of which 2 are reserved for closeout alone.

Typical 5 queries per leg, worst case 8. At 50 legs/month that's about
$1.25, worst case $1.75.

## Cost control

`/flights/{ident}` costs $0.005 per result set; `/schedules` costs $0.02,
which is why deadhead carrier resolution is done once per leg and stored.
At ~50 legs/month and 5-8 queries per leg that's roughly $1.25-$1.75/month,
inside the Personal tier's $5 free credit. `MAX_QUERIES_PER_LEG` (10) caps
any single leg, `ARRIVAL_RESERVE` (2) of those are held for confirming
gate-in, and the per-pilot monthly limit is a hard stop — queries cease
entirely once it's reached, so the app can never quietly produce a bill.

That limit is set by each pilot in Settings ("Monthly spend limit"), stored
on the `users` row, and defaults to $4.50 —  just under the Personal tier's
$5 free credit, so an estimate that's slightly off can't produce a bill.
`AEROAPI_MONTHLY_BUDGET` only supplies the fallback for a row that has no
value. A limit of $0 stops all AeroAPI queries while keeping the key saved;
live ADS-B tracking is free and is never affected.

Spend shown in Settings is **FlightAware's own figure and nothing else**,
read hourly from the free `GET /account/usage`. A local estimate used to be
shown beside it, but two numbers for one thing invites the question of which
to believe, and the estimate was the wrong one — it prices every query at
the `/flights` rate and undercounts any leg that needed `/schedules`. Until
a reading arrives the page says so rather than showing a number.

Enforcement is separate and more paranoid than the display: a fresh reading
is used as-is, and a stale or missing one falls back to the higher of the
last reading and the local count. A stale figure is a floor, not a ceiling
— querying has continued since. That fallback never reaches the screen, and
it exists so an unreachable usage endpoint can't quietly disable the one
control that prevents a bill.

The limit is **always enforced**. The old "keep querying past $X" toggle
was removed in v4.6: the one setting that exists to prevent a surprise bill
should not itself be switchable off. Anyone on a paid tier who doesn't mind
the overage raises the number instead, which is the same outcome stated
honestly. The `aeroapi_allow_overage` column remains on the table because
migrations here are append-only; nothing reads it.

Note that the local estimate prices every query at the `/flights` rate, so
a leg that needed a `/schedules` lookup is undercounted by about $0.015.
That's one more reason to prefer FlightAware's own figure below.

Spend is taken from FlightAware's OWN meter where possible:
`GET /account/usage` is free, is polled at most every 20 minutes, and
replaces the local estimate. Their figure updates every 10 minutes rather
than in real time, so anything older than six hours is treated as stale and
the estimate takes over. Settings shows the poll count and dollars against
the cap, when the figure was last pulled, and which source it came from.

## Storage

Everything lives in `data/flighttracker.db` (SQLite), in the three tables
described under *How the data is organised*.

### Upgrading from v4

The migration runs automatically on first boot and is **idempotent** —
restarting the container repeatedly will not duplicate anything.

**Carries over:** your accounts and settings, your schedule, and every
flown track. Those are irreplaceable — the schedule was typed in and the
tracks were observed.

**Does NOT carry over:** the airline enrichment and closeout blobs. They
are at most 30 days old, they can be re-fetched, and mapping two nested
JSON documents into eighty-odd columns is a one-off guess. Practically:
past flights will show their route and their flown path but not their gate
times until they are re-flown. Everything from the deploy forward is
complete.

**Nothing is dropped except two genuinely dead tables** — `aircraft`, and
the old user-scoped `positions`, neither of which had been read or written
in several versions. The v4 tables that held real data (`legs`,
`flight_tracks`, `flight_aircraft`, `flight_enrichment`, `flight_closeout`)
are deliberately LEFT IN PLACE rather than deleted. If anything about the
migration turns out wrong, the original data is still sitting there to
recover from. They can be dropped by hand once you're happy.

One migration trap worth remembering: v4 had a dead `positions` table with
a completely different shape, and v5 reuses that name. `CREATE TABLE IF NOT
EXISTS` silently did nothing against the old one, leaving every write to
fail on a missing column. `db.py` now checks the shape and drops the old
table first. **This class of bug — a name reused across schema versions —
is invisible on a fresh install and only appears on a real upgrade,** which
is exactly why the migration is tested against a synthetic v4 database
rather than assumed to work.

`data/secret_key.txt` signs login session cookies. It's generated once and
must stay stable — deleting it logs everyone out.

## Deploy on TrueNAS with Dockge

1. Copy this folder to your TrueNAS box (e.g. into the app-data dataset Dockge watches, or wherever you keep your stacks).
2. In Dockge, create a new stack pointing at this folder, or paste `docker-compose.yml`'s contents into a new stack.
3. Make sure the `./data` folder exists on the host (Dockge/Compose will create it as a bind mount if it doesn't).
4. Start the stack. First boot will build the image and expose port `8000`.
5. Visit `http://<truenas-ip>:8000/` — you'll land on `/setup` to create your pilot account.
6. Nothing to configure for live tracking — Airplanes.live needs no API key or account.
7. On `/admin`, grab the 5-digit share code and send it to whoever should be able to view your flights.
8. Everything in `./data` persists across container restarts/rebuilds since it's a bind-mounted volume.

Login is real now (password-protected pilot account, code-gated viewer
access), but there's still no HTTPS/TLS built in — if you're exposing this
beyond your LAN, put it behind Tailscale, a reverse proxy with TLS, or a VPN
so credentials aren't sent in the clear.

## Updating

Dockge's Deploy/Update button won't rebuild the image just because the code
changed — it's a known limitation when a stack uses `build: .` instead of a
pre-built `image:`. Use `update.sh` instead, from the stack's directory on
the TrueNAS host:

```bash
bash update.sh
```

This resets the working copy to match GitHub exactly (`git fetch` +
`git reset --hard origin/main`, not a plain `git pull`), then rebuilds and
restarts the container, tailing the logs so you can confirm it started
cleanly. Using `reset --hard` instead of `pull` means it can never fail
with a "divergent branches" error, even if something was committed locally
on the host and never pushed.

(First run: `chmod +x update.sh` if you want to run it as `./update.sh`
instead — GitHub's browser uploader doesn't preserve the executable bit,
so `bash update.sh` is the safe way to invoke it either way.)

## Version history

### v5.0 — the data rebuild

The pilot's read on v4 was that ADS-B and AeroAPI had been "glued together
to work between the two providers", and that the phase tags were often
wrong. Both were right. This release rebuilds how data is stored and who is
allowed to decide things.

**Seven tables became three.** `legs`, `flight_aircraft`,
`flight_enrichment` and `flight_closeout` collapse into one `flights` row
per leg, with a named column for every fact. `aircraft` and the old
user-scoped `positions` were dead and are dropped. `flight_tracks` becomes
`positions`. The glue is gone because reconciliation moved from DISPLAY
time to WRITE time: the poller decides once and writes it down, and the
page renders the row.

**The page stopped writing to the database.** `compute_live_payload` used
to fetch live data, record track points and advance the aircraft state
machine on every render and every browser poll. Two engines, two clocks,
and whichever got there first changed the answer. That is where the
ordering bugs came from. It is now read-only.

**One badge became two pills.** Phase (always blue, forward-only) and
status (the only colour, blank when there's nothing to say). Diversions no
longer compete with position for one slot.

**Bugs fixed:**

* **Phase fell backwards on a coverage gap.** "In air" → "Unknown"
  mid-cruise, which read to the family as the app losing him. Phase is now
  forward-only and signal loss shows as a note. The Unknown phase is gone.
* **"Delayed" fired on observed lateness.** A 12-minute pushback lit the
  pill when nothing had gone wrong. It now requires the AIRLINE to have
  pushed a time past the FFDO figure. The lateness note is separate and
  still honest.
* **The backstop could close a delayed flight before it departed.**
  *(Found by the pilot.)* It anchored on the SCHEDULED arrival with no
  airline data, so a 3-hour delay expired the clock around pushback — and
  its quiet test passed when there had been no signal ever, i.e. at any
  outstation with no receiver. It now cannot start until the flight has
  demonstrably begun, and anchors on observed departure plus block time.
* **An observed arrival could be read off an aircraft that never moved.**
  *(Found by the pilot.)* Stationary-and-quiet at the departure gate looks
  identical to blocking in. It now requires wheels-on first.
* **Closeout hung with an API key.** Only `actual_in` could close a leg —
  the OOOI field most often missing. A confirmed landing plus 5 min stopped
  and 8 min silent now closes it as `observed`, and a late gate-in upgrades
  it.
* **`flight_closeout` was declared twice in `db.py`** with two different
  shapes; the second never applied, and its `closeout_queries` /
  `last_closeout_at` columns were read by nothing. Moot now.
* **Re-pasting a schedule wiped observed data.** `save_schedule` deleted
  every row first, throwing away the aircraft lock and every observed time
  for legs that hadn't changed. It now updates in place.
* **`Landing` was unreachable without an AeroAPI key.** *(Found by the new
  test suite.)* SQLite stores booleans as `0`/`1`, and the phase code
  tested `is False`, which never matches `0`. Every airborne aircraft fell
  through to the airline-data branch instead of using its position. A
  one-line normalisation, and a nasty one to have found live.
* **The poller used two clocks in one sweep.** `get_current_info` read the
  wall clock while the preview window used the sweep's own `now`, so the
  two could disagree about which leg was current. `poll_once(now)` now
  takes the clock as an argument.
* **The v4→v5 migration hit a name collision.** v5 reuses the name
  `positions` for a table v4 had with a different shape, so `CREATE TABLE
  IF NOT EXISTS` silently did nothing and every write failed on a missing
  column. Invisible on a fresh install; only appears on a real upgrade.
  `db.py` checks the shape and drops the old table first.

**Documentation drift corrected:** the README said a 15-minute query floor
(code: 20) and 3 closeout tries (code: 2).

**Tests went from 33 to 106,** including a new end-to-end suite that flies
a whole leg with a scripted ADS-B feed, and a migration test against a
synthetic v4 database.

### v4.7

* **Never package `data/secret_key.txt`.** The v4.6 zip shipped one, which
  overwrites the key that signs session cookies and logs out the pilot and
  every share-link viewer at once. `.gitignore` covers the file, but that
  only helps if it isn't already tracked — check with
  `git log --oneline -- data/secret_key.txt`. If it has ever been
  committed, `git rm --cached data/secret_key.txt`, push, delete it on the
  box and restart so a fresh key is generated.
* **A wrong password no longer clears the login form.** The username is
  re-rendered and focus moves to the password field. The password itself is
  deliberately not echoed — it would land in the page source, browser cache
  and any proxy log, and the browser's password manager refills it anyway.
* **Calendar agenda rows stay on one line.** They were printing full zoned
  times ("5:15 PM MST"), which overflowed a phone and wrapped the route
  onto a second line. Short times now, with the flight side truncating
  before the times do.
* **Days within a trip are separated again.** `seamless-after` removed the
  gap so a multi-day trip read as one continuous bar, which is right, but
  with no divider the days ran into a single slab. A hairline rule and more
  padding separate them without breaking the bar.
* **Expand/collapse and the past-flights toggle work again** — see below.
* **Spend is FlightAware's number only.** The local estimate is gone from
  the UI, `/account/usage` is polled hourly, and the settings block that
  showed two competing figures has been rebuilt as one line.

### Restored missing JavaScript (v4.7)

`togglePast()` and the card expand/collapse handler had no code behind them
as of v4.5: the markup and CSS were intact, but nothing set the `.open` and
`.expanded` classes and the button's inline `onclick` pointed at a function
that no longer existed. Both were rewritten against the CSS contract, which
specifies them exactly (`.expand-details.open`, `.expand-wrap.open`,
`.collapsed-card.expanded`, `#past-section.open`).

The card header is now a tap target as well as the small hint text, and
taps on anything interactive inside it — a time bubble, the FR24 button, a
flight row — are excluded so they don't collapse the card underneath the
finger.

### v4.6

* **Monthly spend limit is now a number the pilot sets**, replacing the
  keep-querying-past-the-cap toggle. See Cost control above.
* **Times no longer wrap mid-chip.** `.chip-delay` used to be a fourth flex
  sibling inside `.time-chip`, so a long note like "1h 46m late" widened the
  arrival chip until flex shrank both chips and their text wrapped — which
  is what split "PHX" from "7:03 PM" onto separate lines. Code and time now
  sit in a `white-space: nowrap` `.chip-line`, with the delay note stacked
  beneath inside `.chip-body`. If a card is ever genuinely too narrow the
  whole arrival chip drops to the next line, which stays readable.
* **Tap a time to see its zone.** Card times are local to their own airport
  and printed without a zone suffix on purpose — repeating MST/CDT on every
  one is what crowded these rows originally. A small bubble now puts the
  zone one tap away. It reuses the zoned strings already computed for the
  detail rows, so there is no extra AeroAPI cost. `applyTime()` refreshes
  `data-full` alongside the visible text; otherwise a delayed flight would
  pop its ORIGINAL scheduled time.

## Notes

- **`viewer.html` is missing JavaScript as of v4.5 — see below.** Two
  behaviours have no code behind them: nothing toggles `#expand-details` /
  `#expand-wrap`, and `togglePast()` is called by an inline `onclick` but
  never defined. The markup and CSS for both are intact. A quick audit,
  worth repeating after any large template edit:

  ```bash
  grep -c "function togglePast" templates/viewer.html   # must be 1
  grep -c "expand-details'" templates/viewer.html       # must be >0
  ```

  A related trace of the same failure sits above `applyEnrichment()`, where
  the docstring for `selectLeg()` has been absorbed into the following
  comment block. This is the recurring colliding-edit mode: verify after
  every multi-part edit.
- All times shown are **local to the airport**. On the collapsed card the
  zone abbreviation is behind a tap (see v4.6); the expanded detail rows
  still print it inline.
- Callsigns / tracking links use the **ENY** prefix.
- **On time means exactly on time.** `ON_TIME_TOLERANCE_MIN` is 0, so a
  one-minute-late departure reads as late and is tinted red. An earlier
  5-minute grace meant the card printed 5:59 beside a crossed-out 5:57 and
  called it on time, which is an argument with itself.
- **Past flights keep their detail.** Actual times, gates and the frozen
  closeout record stay visible after a leg ages out of the current window
  — useful when a spouse is driving to the airport for a pickup. Nothing
  recomputes and no query is spent; it's all read from disk.
- Taxi-out/Taxi-in/Landing phase detection depends on each airport having
  enough ADS-B ground coverage to see it — busier fields (DFW) are reliable,
  smaller regional stations are a toss-up. When there's no coverage for a
  phase, it's skipped silently rather than guessed.
- No payments, public signup, or email/SMTP integration yet — those are
  intentionally deferred, not missing by accident.

## Tests

106 tests across four suites. Run any of them from this directory; each
uses its own scratch database via `PT_DB_FILE`, so they never touch the
real one and can't leak state into each other.

```bash
python tests_flight_row.py          # 43 — the row, the tags, closure, retention
python tests_poller_end_to_end.py   # 27 — a whole flight, gate to gate
python tests_past_leg_detail.py     # 19 — past-leg and T-30 preview rendering
python tests_budget_limit.py        # 17 — the monthly spend cap
```

`tests_poller_end_to_end.py` is the one to read first if you want to
understand the app. It replaces the ADS-B feed with a scripted one and
walks a single leg through pushback, taxi, climb, cruise, **a total loss of
coverage**, re-acquisition, approach, landing, taxi-in and blocking in,
asserting the pills and the closure decision at every step. The
coverage-gap step is the point of the whole exercise: that is the v4 bug
where the phase fell to Unknown mid-cruise and the card told his family the
app had lost him.

Three fixture notes worth keeping, each of which cost time to find:

- **`dep_time_local` is local to the ORIGIN airport.** Building it from UTC
  clock hands puts a PHX leg seven hours out and silently moves it outside
  the query window, which makes a blocked call prove nothing.
- **A leg needs a ROW in `flights` before `refresh()` will do anything.**
  It reads its query counters and last-query time from there. A leg that is
  only a Python object gets correctly declined — which made
  `tests_budget_limit.py` pass for the wrong reason until the fixture was
  fixed.
- **`poller` does `from .livesource import live_state`,** which binds the
  name at import. A fake feed has to be installed on `poller.live_state`,
  not on `livesource.live_state`.

One fixture note worth keeping: `dep_time_local` is local to the ORIGIN
airport. Building it from UTC clock hands puts a PHX leg seven hours out
and silently moves it outside the query window, which is exactly how a
budget test can pass for the wrong reason.

It writes to a scratch database via the `PT_DB_FILE` environment variable
and never touches `data/flighttracker.db`. **Keep test files inside this
folder** — the earlier suites lived one directory up and were lost when the
project was packaged, which is why there's only one here.
