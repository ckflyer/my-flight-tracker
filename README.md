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

Status uses OOOI first and ADS-B as fallback — `actual_out` is reportedly
absent 15-50% of the time, so the two complement rather than compete.
Enrichment also gives POSITIVE flight identification: AeroAPI knows which
record corresponds to this origin/destination pair, so a turn's two
directions are distinguished by data rather than inference.

## Storage

Everything — schedule legs, users/accounts, and live
breadcrumb positions — lives in `data/flighttracker.db` (SQLite). If you're
upgrading from a version that used `data/schedule.json` or
`data/settings.json`, those get imported automatically and attached to
whichever account you create first via `/setup`.

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
- Taxi-out/Taxi-in/Landing phase detection depends on each airport having
  enough ADS-B ground coverage to see it — busier fields (DFW) are reliable,
  smaller regional stations are a toss-up. When there's no coverage for a
  phase, it's skipped silently rather than guessed.
- No payments, public signup, or email/SMTP integration yet — those are
  intentionally deferred, not missing by accident.
