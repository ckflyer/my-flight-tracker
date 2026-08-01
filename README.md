# Flight Tracker

Simple personal flight schedule + live tracking helper for airline pilots (Envoy / American Eagle).

- Paste your FFDO schedule
- Automatic “current / next” flight based on local block times
- One-tap FlightRadar24 & FlightAware links (ENY prefix)
- Timezone-aware (airport-local times)
- Designed to self-host on TrueNAS / any Linux box

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

Then open:

- **Viewer** (girlfriend): http://localhost:8000/
- **Admin** (you): http://localhost:8000/admin

## Schedule format

Paste exactly like this (one leg per line):

```
06/26/2026 3729 DFW 1742 OKC 1837
06/26/2026 3729 OKC 1911 DFW 2011
06/26/2026 3566 DFW 2227 ICT 2351
```

`MM/DD/YYYY  FLIGHT  ORIG  DEPTIME  DEST  ARRTIME`

Times are local block times at each airport.

## Project layout

```
flight-tracker/
├── app/
│   ├── main.py          # FastAPI routes
│   ├── models.py        # data models
│   ├── parser.py        # FFDO text parser (unchanged)
│   ├── airports.py      # IATA → timezone / ICAO lookup
│   ├── schedule.py      # load/save (SQLite) + current-flight logic
│   ├── db.py            # SQLite connection + schema + legacy JSON migration
│   ├── aircraft.py      # aircraft table: auto-noted from live tracking, editable in /admin
│   ├── track.py         # breadcrumb positions + progress % / ETA math
│   ├── opensky.py       # live ADS-B lookups
│   └── settings.py      # app settings (still JSON — data/settings.json)
├── templates/
│   ├── viewer.html      # mobile-friendly tracker view (map, breadcrumb, progress, ETA)
│   └── admin.html       # schedule import + aircraft info editor
├── data/                # flighttracker.db + settings.json live here (gitignored)
├── static/              # empty on purpose, just needs to exist for FastAPI's StaticFiles mount
├── Dockerfile
├── docker-compose.yml   # Dockge-ready stack
├── requirements.txt
└── README.md
```

## Storage

Schedule legs, the aircraft table, and live breadcrumb positions are all in
`data/flighttracker.db` (SQLite). If you're upgrading from an older version
that used `data/schedule.json`, it's imported into SQLite automatically the
first time the app starts — no manual step needed.

`data/settings.json` (OpenSky credentials, theme, poll interval, etc.) is
unchanged.

## Aircraft info

There's no reliable free API for ICAO24 → registration/type lookups, so the
`aircraft` table is self-maintained: the first time a tail is seen via live
tracking it's added automatically (blank), and you can fill in the
registration/type/notes on the `/admin` page. Once filled in, it shows up on
the tracker view whenever that aircraft is live.

## Deploy on TrueNAS with Dockge

1. Copy this folder to your TrueNAS box (e.g. into the app-data dataset Dockge watches, or wherever you keep your stacks).
2. In Dockge, create a new stack pointing at this folder, or paste `docker-compose.yml`'s contents into a new stack.
3. Make sure the `./data` folder exists on the host (Dockge/Compose will create it as a bind mount if it doesn't).
4. Start the stack. First boot will build the image and expose port `8000`.
5. Open `http://<truenas-ip>:8000/settings` and paste in your OpenSky Client ID / Secret (this is simpler than editing the compose file, since it's stored in `data/settings.json` and survives rebuilds). Alternatively, set `OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET` in the stack's environment variables in Dockge.
6. Everything in `./data` (schedule, aircraft info, settings) persists across container restarts/rebuilds since it's a bind-mounted volume.

No authentication is built in — put it behind Tailscale, your reverse proxy, or a VPN if exposing beyond your LAN.

## Notes

- All times shown are **local to the airport** + timezone abbreviation.
- Callsigns / tracking links use the **ENY** prefix.
- No authentication yet – put it behind Tailscale, VPN, or a reverse-proxy password when exposed.
