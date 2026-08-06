# Pilot Tracker

Personal flight schedule + live tracking app for airline pilots (built around
Envoy / American Eagle's FFDO schedule format) and the people waiting on
them. Think "Flighty, but for crew and family" — no social features, no
stats/gamification, just the tracking.

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
flight-tracker/
├── app/
│   ├── main.py          # FastAPI routes, auth guards
│   ├── auth.py           # password hashing, sessions, share codes, user CRUD
│   ├── models.py        # data models
│   ├── parser.py        # FFDO text parser
│   ├── airports.py      # IATA → timezone / ICAO lookup
│   ├── schedule.py      # schedule storage + current-flight logic (per user_id)
│   ├── db.py            # SQLite connection + schema + migrations
│   ├── track.py         # breadcrumb positions, flight-phase state machine, progress/ETE math
│   ├── airplaneslive.py # Airplanes.live provider (swap this to change source)
│   ├── livesource.py    # provider-agnostic front door: shared cache + rate limit
│   ├── flightmatch.py   # which aircraft is actually this leg (hex lock, arbitration)
│   ├── aeroapi.py       # FlightAware AeroAPI client (/flights, /schedules, /account/usage)
│   ├── enrichment.py    # OOOI, delays, gates, query schedule, spend tracking
│   ├── closure.py       # positive closeout: frozen arrival record
│   ├── carrier.py       # deadhead operator resolution
│   ├── poller.py        # background track recorder + T-30 preview sweep
│   └── settings.py      # per-user settings (stored on the users table)
├── templates/
│   ├── viewer.html      # mobile-friendly tracker view (map, breadcrumb, progress, radar)
│   ├── admin.html       # schedule import/delete + share-code management
│   ├── settings.html    # display preferences
│   ├── login.html       # pilot login / viewer code entry
│   └── setup.html       # first-run pilot account creation
├── data/                 # flighttracker.db + secret_key.txt live here (gitignored)
├── static/                # app icons/manifest for "Add to Home Screen"
├── Dockerfile
├── docker-compose.yml    # Dockge-ready stack
├── update.sh             # pulls latest + forces a rebuild (see Updating below)
├── tests_past_leg_detail.py  # regression cover, runs against a scratch DB
├── tests_query_schedule.py   # asserts the per-trigger AeroAPI caps hold
├── requirements.txt
└── README.md
```

## Live data source

Live positions come from the [Airplanes.live](https://airplanes.live/api-guide/)
REST API. No key, no account, nothing to configure. It's rate limited to 1
request per second for the whole deployment, so `livesource.py` puts a shared
cache in front of it: everyone watching the same flight shares one upstream
lookup, no matter how many family members have the page open.

To check the source is reachable and returning what the app expects, run this
on the server with a callsign that is airborne right now:

    python3 check_live_source.py ENY3729

To swap providers, write a module exposing `fetch_state(callsign)` returning
the normalized dict documented at the top of `app/livesource.py`, then change
the single import there. Nothing else in the app knows which service is in use.

## Flight tracks

Tracks are recorded by a background thread (`app/poller.py`) that sweeps
every ~20s for flights inside their scheduled window and records position
**whether or not anyone is watching**. Before v2.6, recording only happened
while someone had the page open — which meant the pilot, the one person
guaranteed not to be watching, got no track of his own flights. The phase
machine reads the same history, so "Arrived" also depended on someone
looking.

Tune with `TRACK_POLLER_INTERVAL_S` (seconds, default 20) or disable with
`TRACK_POLLER_ENABLED=0`. With nothing scheduled it makes no network
requests at all. The container runs a single uvicorn worker, so there is
exactly one poller per deployment.

Tracks are keyed by FLIGHT, not by user: `flight_tracks.flight_key` is the
leg id with any `-DH` suffix stripped. Two pilots on the same flight share
one track rather than storing duplicate copies, and a deadhead leg and a
working leg on the same flight record into the same path. Which flights a
user is on stays private in `legs`, which is still user-scoped.

Each flight keeps one flown path. Points are thinned on write (a fix is
skipped unless the aircraft moved at least ~0.12 nm from the last stored
one, though ground-state changes are always kept), so a plane parked at a
gate stores one row instead of one per poll. Tracks older than 30 days are
pruned automatically — see `TRACK_RETENTION_DAYS` in `app/track.py`.

Tapping a past flight draws its real track. A flight with no stored track
falls back to a dashed straight line between the airports.

## Matching the right aircraft

Live data is looked up by callsign, and a callsign is not unique to a leg.
Regional turns fly out and back under one flight number (3700 DFW-MFE and
3700 MFE-DFW the same day), and the return departs well inside the 3-hour
window the outbound leg stays "current" for.

`app/flightmatch.py` solves this with aircraft IDENTITY and the schedule,
never geometry:

  0. **Arbitrate** — when several legs share a callsign on the same day
     (a turn, or a multi-stop line), only the one actually in progress may
     claim the aircraft: the latest leg whose scheduled departure has
     arrived. This is deterministic and needs no observation of the
     aircraft at all, which matters because the ground-cycle release below
     depends on seeing it stopped at the outstation — and small fields
     frequently have no ADS-B coverage, so that release often never fires.
     Not scoped to one account: which leg an aeroplane is flying is a fact
     about the aeroplane.

  1. **Acquire** — a leg adopts an aircraft on its callsign when that
     aircraft is at the ORIGIN, or during a window around scheduled
     departure (`ACQUIRE_BEFORE_DEP_MINUTES`/`ACQUIRE_AFTER_DEP_MINUTES`,
     default -20/+45). The window matters for outstations with no ADS-B
     receiver, where a flight may first appear already enroute. A turn's
     return leg can't depart until this one has landed, so it never falls
     inside that window.
  2. **Hold** — from then on only that ICAO hex is accepted, and it's
     accepted unconditionally, wherever it goes.
  3. **Release** — a leg is one ground -> airborne -> ground cycle. Once the
     acquired aircraft has flown and then been STOPPED on the ground
     (under 5 kts) for `GROUND_COMPLETE_SECONDS` (default 300), the leg is
     finished and the callsign is released. Stopped rather than merely on
     the ground, so a long taxi-in doesn't end the leg early and lose the
     taxi track. Completion doesn't require reaching the scheduled
     destination, so a diversion correctly ends the leg where it actually
     lands.

     A change of transponder code CORROBORATES this, but only while the
     aircraft is stopped on the ground — a new code parked at a gate means
     a new flight plan, so the previous leg is over and there's no need to
     wait out the full parked window. Squawk is never consulted in the
     air: codes are routinely reassigned in flight as an aircraft is handed
     between ATC facilities, so an airborne change says nothing about
     whether the flight has ended. Only ground-observed codes are even
     stored.

Judging aircraft by heading was tried and removed. It cannot survive a
diversion: a DFW-OKC leg turning back to DFW looks identical to the return
flight, so the guard disowned the user's own aircraft exactly when it
mattered most. Holds, opposite-flow departures and arrival vectoring break
it the same way. There is no route data to compare against either — ADS-B
does not transmit origin or destination, and airplanes.live has no route
endpoint.

## Optional: FlightAware AeroAPI enrichment

Off by default. Each pilot enables it in Settings with their OWN AeroAPI
key — the Personal tier includes $5/month of free queries, $10 if you feed
ADS-B. With no key the app behaves exactly as it does without it.

It supplies only what ADS-B cannot broadcast: actual OOOI times (gate-out,
wheels-off, wheels-on, gate-in), live arrival estimates, delays,
diversions with the amended destination, cancellations, and gate/terminal.
Position, altitude, speed, tail number and type stay on the free ADS-B
feed — they're never bought.

Cost discipline, in `app/enrichment.py`:

  * ONLY the background poller queries. Page renders read the cache, so
    refreshing during a delay costs nothing.
  * ADS-B transitions are the primary trigger — a query is spent when
    something changed, not on a timer.
  * A schedule fallback covers legs with no ADS-B coverage at all, which
    is exactly when the airline's own OOOI matters most.
  * `MIN_QUERY_GAP` and `MAX_QUERIES_PER_LEG` cap the spend per leg no
    matter what.

Budget works out around 4-6 queries per flight. Settings shows a running
count for the month.

Before trusting it, probe the real API from the server:

    docker compose exec flight-tracker python3 check_aeroapi.py YOUR_KEY ENY3729 DFW OKC

That makes one query and prints what came back, what the app would show,
and which fields are unpopulated for your airline.

Delays are reported separately for departure and arrival, because "is he
getting out?" and "when does he get there?" are different questions and a
late pushback doesn't always become a late arrival. Both are measured
against the FFDO SCHEDULE, not the airline's published times — the pilot
flies to the bid line, so that's what late means here.

When a revised time exists, the card shows THAT time with the scheduled
one struck through beneath it. Previously it printed the scheduled time
next to a note saying "18 min early", and the two never agreed. Delta and
displayed time are both derived from minute-truncated values so they can
never disagree by a rounding minute.

Status combines OOOI and ADS-B by RANK, taking whichever is further along.
The two fail in opposite directions: OOOI runs late (actual_on and actual_in
are published with a lag, so a landed flight still reads "In Air"), while
ADS-B runs blind (no receiver near a small field means no ground state at
all). Ranking them means whichever notices first wins and neither can drag
the flight backwards. An earlier version returned the first matching OOOI
field and never consulted ADS-B again, which left flights showing "In Air"
after they had visibly landed.

## Deadheads on other carriers

An FFDO line gives a bare flight number and a "(D)" — never the airline. A
deadhead is usually mainline American or another wholly-owned regional,
each broadcasting its own callsign, so assuming ENY looks up a flight that
doesn't exist and the leg never tracks.

`app/carrier.py` resolves it from flight number CROSSED WITH THE ROUTE,
since only one carrier flies 4110 DFW-LFT on a given day. With AeroAPI it
uses GET /schedules (one query, ever — schedules don't change, so the
answer is stored on the leg). Without a key it probes ENY/AAL/JIA/PDT
against ADS-B once around departure and takes whichever has an aircraft at
the origin. Codeshares resolve to the OPERATING carrier, since that's what
gets broadcast.

Old note (superseded): status uses OOOI first and ADS-B as fallback — `actual_out` is reportedly
absent 15-50% of the time, so the two complement rather than compete.
Enrichment also gives POSITIVE flight identification: AeroAPI knows which
record corresponds to this origin/destination pair, so a turn's two
directions are distinguished by data rather than inference.

## The FFDO line is the source of truth

Everything on the card is measured against the schedule the pilot pasted
in, never against the airline's published one. `departure_delay` and
`arrival_delay` both take `leg.dep_datetime_utc()` / `leg.arr_datetime_utc()`
as their baseline; AeroAPI's `scheduled_out` / `scheduled_in` are stored
but never used as a reference point.

This matters because airlines amend published schedules mid-day. If a
flight bid at 5:57 is republished at 6:40, that is a 43-minute DELAY and
the card says so — it is not a new "scheduled" time, and the struck-through
figure does not move. The same rule covers leaving early. The FFDO number
lives in our own `legs` table where nothing external can revise it.

An earlier design also snapshotted the airline's first published times
(`flight_enrichment.first_seen`) so amended figures could be compared
against what was originally advertised. That answered a question this app
doesn't ask, and it's no longer written — the column remains only because
SQLite column drops mean a table rebuild.

## Arrival times: fact before forecast

Arrival is taken in this order: the airline's actual gate-in, then OUR OWN
observed gate stop (the aircraft came to a halt after flying — see
`observed_gate_in`), then the estimate. AeroAPI publishes actual_in with a
lag, so a flight can be parked while the only airline figure available is
an hour-old forecast; presenting that forecast as the arrival time is how
"arrived 4:05, 11 minutes early" appeared for a flight that blocked in at
4:11.

Tense follows the FLIGHT rather than the API: if status says Arrived, the
note reads "Arrived", never "Arrives".

"Departing" on the ADS-B side only means the scheduled departure time has
passed with nothing seen yet — it is not evidence the aircraft is moving.
When the airline has pushed the estimate by more than
`DELAY_STATUS_MIN` (10) minutes and there's still no gate-out, the status
reads **Delayed** instead.

## The phase machine does not guess

`compute_phase` reports only what the aircraft is broadcasting:

  Scheduled -> Taxi-out -> In Air / Landing -> Taxi-in, else **Unknown**

Earlier versions inferred a phase from the clock and from silence:
scheduled departure had passed so the flight was "Departing"; the signal
dropped while airborne so it "must still be flying"; it dropped on the
ground so it "must have arrived". Air travel doesn't cooperate — gate
holds, returns to stand and diversions all break that, and a coverage gap
is not evidence of anything. When the aircraft isn't tracked the card says
**Unknown** and when it was last seen, and tracking resumes wherever the
aircraft reappears.

"Departing" is gone. "Arrived", "Diverted" and "Cancelled" never come from
here: arrival is a closure decision (closure.py), and diversion and
cancellation exist only in the API — ADS-B has no concept of either.

Progress, distance and ETE follow the same rule. No live position means no
figure at all and the progress bar hides, rather than showing a number
derived from the clock. ETE needs either a live groundspeed or the
airline's revised arrival; the bare schedule isn't knowledge.

Landing fires inside 8 nm (was 17, which triggered while still being
vectored downwind).

## Progress and status ordering

`compute_live_payload` computes the ADS-B phase FIRST, then lets enrichment
refine it. That assignment used to happen at the END of the function, after
the enrichment block had already set the status — silently discarding every
OOOI-derived value including "Delayed". A flight two hours late at the gate
still read "Departing" because the airline's own view was computed and then
thrown away.

Progress is pinned to zero until there is evidence the aircraft actually
left: a live fix showing not-on-ground, or a status of In Air or later.
`compute_progress` once fell back to measuring elapsed clock time against
the SCHEDULE when there was no live fix, so a flight still at the gate
showed 27% en route and, past its scheduled arrival, 100%. That fallback
was removed in v4.0 — with no live position there is now no progress figure
at all, and the bar simply isn't drawn. The guard is kept because it also
covers the case where a fix exists but the aircraft hasn't moved. When
revised times are known, progress and ETE are recomputed against those
rather than the superseded schedule.

## Closing a leg out

`app/closure.py` makes closure one decision with one recorded reason,
stored in `flight_closeout` alongside the flown track. Once closed a leg is
FROZEN: no polling, no live data, no recomputation, so a past flight's
numbers never drift.

With an AeroAPI key, OOOI is the authority — the airline's `actual_in` (or
a cancellation) closes the leg, and nothing else does. Without a key,
ADS-B does the best it can: the ground cycle (flew, then stopped), or the
aircraft going airborne AGAIN after landing, which is unambiguous and needs
no timers.

A CLOSEOUT PASS keeps asking for `actual_in` once the aircraft is down —
every 10 minutes for up to 90, using queries reserved from the per-leg
budget so a chatty flight can't starve it. Without it the ordinary triggers
can run out before gate-in publishes and the leg never closes on the
airline's authority.

An observed arrival requires BOTH that the aircraft has been stationary for
5 minutes AND that its signal has gone quiet for 8. Stopping alone means
nothing — waiting off-gate for a stand can mean half an hour parked with the
transponder still transmitting. An aircraft that has genuinely blocked in
goes silent.

A BACKSTOP exists because `actual_in` is the field most often missing, and a
leg that never closes never releases its callsign. It is deliberately hard
to reach: 3 hours past the REVISED arrival (not the scheduled one, since a
six-hour delay is a real and normal thing), and only when there is nothing
left to learn — no live ADS-B signal and no fresh airline data. It records
`backstop` rather than claiming airline authority, and a late gate-in still
upgrades it.

Every figure carries its source, so the card can say "arrival from airline"
versus "observed" rather than presenting a guess as fact.

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
    GROUND past its departure time. One prompt check at T+20, then a slower
    watch, `MAX_DELAY_WATCH_TRIES = 3` in total. It stops the moment the
    aircraft is seen airborne, handing straight over to the cruise checks
    rather than waiting for the next 30-minute tick.

    The counter that enforces that cap keys off the trigger's reason
    string, and through v4.5 it tested for "T+15" while the trigger
    returned "T+20" — so the count never advanced, the first branch stayed
    true, and a flight stuck at the gate re-asked the same question every
    `MIN_QUERY_GAP` until the per-leg ceiling stopped it. Eight queries
    where three were intended.
  * **3 cruise checks** — evenly spaced between ACTUAL departure and
    estimated wheels-on. They require the aircraft to genuinely be off the
    ground: gated only on an anchor, they fired while a delayed flight sat
    at the gate. Any falling inside the `MIN_QUERY_GAP` floor (20 min)
    are skipped
    outright (the latest due checkpoint wins), so a short leg uses fewer
    rather than firing them all late.
  * **Wheels down + 5** — often already on stand, closing the leg in one
    query instead of several. Fires once. Needs ADS-B. Touchdown is
    DEBOUNCED: on_ground must hold for 60 seconds at under 90 kts before it
    counts, so a single bad alt_baro frame on approach can't trigger it
    early. The recorded time is backdated to the wheels-down moment, not to
    when confirmation finished.
  * **Closeout** — every 10 minutes until gate-in, capped at 2 attempts
    (`MAX_CLOSEOUT_TRIES`).
  * **Arrival fallback** — for legs with NO ADS-B at all, where no
    wheels-down event can ever fire. Capped at 2.

Both repeating triggers are capped because they loop waiting for an answer
that sometimes never comes; uncapped, a missing gate-in ate the whole
per-leg budget. After the caps, the backstop closes the leg instead.

Cruise checks anchor on the airline's wheels-off, or on ADS-B's observed
takeoff when the API hasn't caught up — without that fallback a flight
departing between ground checks has no anchor and cruise checks can't
start at all.

Every trigger has its own cap and none borrows from another, but the caps
added together (1 + 3 + 3 + 1 + 2 + 2) come to more than the ceiling, so
`MAX_QUERIES_PER_LEG = 10` is what actually bounds a pathological leg —
one that is delayed on the ground, then late, then never publishes a
gate-in. `ARRIVAL_RESERVE = 2` of those ten are unavailable to anything
except closeout and the no-ADS-B fallback, so a chatty leg cannot arrive
at its own ending with nothing left to spend.

Typical 5 queries per leg, worst case 8. At 50 legs/month that's about
$1.25, worst case $1.75.

## Cost control

`/flights/{ident}` costs $0.005 per result set; `/schedules` costs $0.02,
which is why deadhead carrier resolution is done once per leg and stored.
At ~50 legs/month and 5-8 queries per leg that's roughly $1.25-$1.75/month,
inside the Personal tier's $5 free credit. `MAX_QUERIES_PER_LEG` (10) caps
any single leg, `ARRIVAL_RESERVE` (2) of those are held for confirming
gate-in, and `AEROAPI_MONTHLY_BUDGET` (default $4.50) is a hard monthly
stop — queries cease entirely once it's reached, so the app can never
quietly produce a bill.

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

Everything — schedule legs, users/accounts, and live
breadcrumb positions — lives in `data/flighttracker.db` (SQLite). If you're
upgrading from a version that used `data/schedule.json` or
`data/settings.json`, those get imported automatically and attached to
whichever account you create first via `/setup`. The import runs ONCE:
`schedule.json` is renamed to `schedule.json.imported` afterwards. Before
v4.6 the guard was "no legs exist", which is also true right after a pilot
deletes their whole schedule on purpose — so the old one silently came
back on the next restart.

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

## Notes

- All times shown are **local to the airport** + timezone abbreviation.
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

`tests_past_leg_detail.py` covers the past-leg and T-30 preview paths
through `compute_live_payload`. `tests_query_schedule.py` walks a flight
that never leaves the gate and asserts the per-trigger caps hold — the
counters in `refresh()` key off trigger REASON STRINGS, so a reworded
trigger silently disables its own cap. Run both from this directory:

```bash
python tests_past_leg_detail.py
python tests_query_schedule.py
```

It writes to a scratch database via the `PT_DB_FILE` environment variable
and never touches `data/flighttracker.db`. **Keep test files inside this
folder** — the earlier suites lived one directory up and were lost when the
project was packaged, which is why there's only one here.
