# Pilot Tracker

Personal flight schedule + live tracking app for airline pilots (built around
Envoy / American Eagle's FFDO schedule format) and the people waiting on
them. Think "Flighty, but for crew and family" — no social features, no
stats/gamification, just the tracking.

- Paste your FFDO schedule; automatic current/next/past flights based on
  local block times
- Live ADS-B position via OpenSky, with a real flight-phase state machine
  (Scheduled → Departing → Taxi-out → In Air → Landing → Taxi-in → Arrived) —
  not just a clock guess
- Breadcrumb trail, flight-progress bar, distance-to-go, ETE, and a classic
  green/yellow/red weather radar overlay on the current flight's map
- Aircraft registration/type looked up automatically (OpenSky's public
  aircraft database) — no manual entry
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
  OpenSky credentials, separate everything per pilot account) as groundwork
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
│   ├── aircraft.py      # aircraft registration/type auto-lookup (OpenSky DB)
│   ├── track.py         # breadcrumb positions, flight-phase state machine, progress/ETE math
│   ├── opensky.py       # live ADS-B lookups
│   └── settings.py      # per-user settings (stored on the users table)
├── templates/
│   ├── viewer.html      # mobile-friendly tracker view (map, breadcrumb, progress, radar)
│   ├── admin.html       # schedule import/delete + share-code management
│   ├── settings.html    # OpenSky credentials + display preferences
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

## Storage

Everything — schedule legs, users/accounts, aircraft metadata, and live
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
6. Open `/settings` and paste in your OpenSky Client ID / Secret (stored per-account, survives rebuilds). Alternatively, set `OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET` in the stack's environment variables in Dockge — the Settings page value takes priority if both are set.
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
