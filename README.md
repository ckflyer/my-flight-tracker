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
│   ├── parser.py        # FFDO text parser
│   ├── airports.py      # IATA → timezone / ICAO lookup
│   └── schedule.py      # load/save + current-flight logic
├── templates/
│   ├── viewer.html      # mobile-friendly tracker view
│   └── admin.html       # schedule import page
├── data/                # schedule.json lives here (gitignored)
├── requirements.txt
└── README.md
```

## Deploy on TrueNAS / Docker later

Example bare-metal:

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Or wrap it in a simple Docker container / systemd service.

## OpenSky (coming next)

Live ADS-B position via OpenSky Network is planned.  
Credentials go in environment variables (never commit them).

## Notes

- All times shown are **local to the airport** + timezone abbreviation.
- Callsigns / tracking links use the **ENY** prefix.
- No authentication yet – put it behind Tailscale, VPN, or a reverse-proxy password when exposed.
