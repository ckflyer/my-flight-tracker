# flight-tracker

Self-hosted flight tracking for airline crew and their families. FastAPI +
SQLite + Jinja, deployed via Docker on TrueNAS/Dockge. Version 5.5.

<!--
READER: THIS FILE IS OPTIMISED FOR AI CONSUMPTION, NOT HUMAN BROWSING.
Terse, dense, decision-oriented. The owner does not read code and does not
need this file to be friendly — he needs the next model to not undo
correct work. Rules stated as INVARIANTS are load-bearing: each one
encodes a bug that already shipped. Do not "simplify" them without
reading the rationale attached.
-->

## AGENT PROTOCOL

**Session start:** read this file top to bottom, then read the code you
intend to change. This file is a map; the code is the territory and may
have drifted. If the packaged zip and the deployed build disagree, stop
and say so before editing — that has already cost a working feature once.

**Session end (required, before packaging):**
1. Bump `app/version.py`.
2. Add a `## VERSION HISTORY` entry at the top of it.
3. Update `## STATE` and `## OPEN`.
4. Record any bug you hit and how it was diagnosed.
5. Run all six test suites. Package only if all pass.
6. Never ship `data/*.db` or `data/secret_key.txt`.

**Owner context:** no programming background. Explain reasoning in prose in
chat, not jargon. He is a line pilot and is the authority on operational
questions (what "delayed" means, when a flight is over) — ask him rather
than inferring. He has caught two real bugs by inspection; take his hunches
seriously.

**Deploy workflow:** he drops extracted files into a GitHub repo, runs
`git pull` on TrueNAS, then `update.sh` via Dockge.

## STATE

v5.1. Deployed target: TrueNAS. Multi-user: the owner plus several FOs,
who fly the same legs — hence shared flight rows (v5.1).

Tests: 176, six suites, all passing.

| Suite | N | Covers |
|---|---|---|
| `tests_flight_row.py` | 50 | write modes, both tag ladders, closure guards, shared crew, retention |
| `tests_poller_end_to_end.py` | 27 | full flight gate-to-gate, scripted ADS-B feed |
| `tests_past_leg_detail.py` | 19 | past-leg + T-30 preview rendering |
| `tests_budget_limit.py` | 17 | monthly spend cap at its enforcement point |
| `tests_carrier_cap.py` | 13 | deadhead lookup cap, FFDO placeholder filter |
| `tests_ui_fixes.py` | 50 | layover labels, untracked phase, sequencing, flight list, time lines |

## OPEN

- **AeroAPI field mapping verified only against a synthetic record.** Wiring
  confirmed end-to-end (gates, times, tail, Delayed pill all land). If
  FlightAware renames a field the failure is SILENT — data just never
  appears. Verify on the box: `python check_aeroapi.py <key> ENY3729 DFW OKC`.
- **v5.1 not yet run on real hardware.** Sandbox only. Back up
  `data/flighttracker.db` before first `update.sh`.
- `/account/usage` response shape unverified against the live endpoint.
  `refresh_usage()` logs an unrecognised shape rather than reporting zero
  spend; grep container logs for it.
- `app/main.py` ~1300 lines, edited surgically in v5.0/v5.1; not fully
  audited. All routes return 200 and all tests pass.
- Tune AeroAPI spend toward the $5 free credit. ~46 legs/month × ~5 queries
  ≈ $1.25; worst case ≈ $2.00. Headroom exists.
- Distribution undecided. Airplanes.live is non-commercial; AeroAPI
  Personal tier is personal-use only.

## DATA MODEL

Four tables in `data/flighttracker.db`. Was seven before v5.0.

```
users     accounts, prefs, AeroAPI key, spend counters
flights   ONE ROW PER REAL-WORLD FLIGHT. SHARED. Not user-scoped.
          id = DATE-FLIGHTNUM-ORIGIN-DEST
roster    (user_id, flight_id) + sort_index, is_deadhead, trip_start
positions breadcrumb trail, keyed by flight id
```

**Split rule:** facts about the AEROPLANE → `flights`. Facts about a
PERSON'S RELATIONSHIP to it → `roster`. Deadheading is the canonical case:
one flight is a working leg for the captain and a deadhead for the FO.

**Why shared (v5.1):** crew fly together. One aeroplane, one takeoff, one
gate-in ⇒ one row. v5.0 gave each pilot a row and fanned writes out to all
of them; that worked but meant two AeroAPI queries for one identical
answer, each pilot's key paying separately. `enrichment.payer_for()` now
picks the lowest user id with an enabled key AND remaining budget; if that
pilot is capped, the next covers it, so the flight does not go dark for
everyone.

There is no `-DH` suffix on flight ids. It described a person, not an
aeroplane. `flights.flight_key()` strips it for legacy input.

### ADS-B and airline values are SEPARATE COLUMNS

| Event | Airline | Observed |
|---|---|---|
| Gate-out | `out_actual_api` | `out_observed` |
| Wheels-off | `off_actual_api` | `off_observed` |
| Wheels-on | `on_actual_api` | `on_observed` |
| Gate-in | `in_actual_api` | `in_observed` |

**Do not merge these.** Separation lets the card state its source, lets
disagreement surface, and stops a lagging airline record overwriting
something observed.

**Display priority:** airline wins for gates, delays, cancellation,
diversion, revised times. For the four events above, **whichever is
further along wins, and neither may move the flight backwards.** The two
sources fail in opposite directions — airline runs late, ADS-B runs blind.
Blanket airline priority reintroduces "In air while visibly parked",
because `actual_on` publishes with a lag.

### Three write modes (`flights.write`)

| Mode | SQL | Use for | Failure if misused |
|---|---|---|---|
| `once` | `COALESCE(col, ?)` | moments that happened: wheels-off, aircraft hex, airline's original schedule | a re-query overwrites truth with a later restatement |
| `latest` | `COALESCE(?, col)` | things that change: position, revised estimates, gates | **blank guard** — an empty poll over a coverage hole erases known state |
| `always` | `col = ?` | recomputed derived values: progress, ETE | stale figures freeze on the card |

## INVARIANTS

Each encodes a shipped bug. Do not remove without reading VERSION HISTORY.

1. **Phase only moves forward.** `tags.advance_phase`. Coverage gap → phase
   holds, plus a "no signal for N min" note. Never regress to Unknown;
   Unknown does not exist.
2. **The page never writes.** `main.compute_live_payload` and `view.py` are
   read-only. Only `poller.py` decides. v4 had two engines on two clocks.
3. **One clock per sweep.** `poll_once(now)` takes the clock as an argument
   and passes it to `get_current_info`.
4. **Delayed requires an airline PUSH,** not observed lateness. Both: (a) an
   estimate/actual differing from the airline's own scheduled time, (b)
   landing later than the FFDO time. Condition (a) prevents a permanently
   lit pill from routine bid-line/published-schedule offsets.
5. **Only closure sets phase = Arrived.** "Stopped" ≠ "flight over".
6. **Closure gated on `has_departed()`.** Backstop and observed-arrival
   cannot fire until ADS-B saw airborne or the airline published
   gate-out/wheels-off. Scheduled time passing is not evidence.
7. **Observed arrival requires wheels-on first.** Stationary+silent at the
   departure gate is identical to blocking in.
8. **Backstop anchors on revised arrival,** else observed departure +
   scheduled block. Never the original timetable.
9. **Progress requires a live fix.** No fix → no figure, bar hides. Never
   derive from elapsed clock time.
10. **Aircraft identity is the ICAO hex, never geometry.** Heading-based
    rejection breaks on diversions, holds and opposite-flow departures.
11. **Booleans from SQLite are `0`/`1`, not `False`/`True`.** Normalise
    before `is False` comparisons.
12. **Never reuse a table name across schema versions** without checking
    `PRAGMA table_info`. `CREATE TABLE IF NOT EXISTS` silently no-ops.
13. **Deleting a user deletes their `roster` rows only.** Flights and
    tracks are shared.

## MODULE MAP

```
main.py         FastAPI routes, rendering. READ-ONLY w.r.t. flight state.
poller.py       background engine, 20s sweep. SOLE decision-maker.
flights.py      shared row + roster: read, write modes, retention
tags.py         phase ladder (forward-only) + status (bidirectional)
view.py         row -> card payload. READ-ONLY.
closure.py      when a leg ends and on whose authority
flightmatch.py  which airframe is flying this leg (hex lock)
enrichment.py   AeroAPI spend triggers, budget, payer selection
track.py        breadcrumbs + progress/distance/ETE maths
livesource.py   shared cache + 1 req/s floor over the ADS-B provider
airplaneslive.py / aeroapi.py / carrier.py   providers
db.py           schema + migrations (v4 and v5.0 -> v5.1)
schedule.py     past/current/upcoming split, and which leg is live
parser.py models.py auth.py settings.py airports.py geo.py ratelimit.py
templates/viewer.html   the app (65KB, edit surgically)
```

## THE TWO PILLS

Independent. Status renders first, phase second.

**Phase** — always theme blue. Forward-only ladder:
`Scheduled → Taxi-out → In air → Landing → Taxi-in → Arrived`.
Landing = airborne within 8nm of destination, or airline `actual_on`
without `actual_in`. Legs with zero ADS-B still get a phase from airline
OOOI.

**Status** — the ONLY coloured pill. **Blank when nothing to say; there is
no "on time" pill** (a badge on every normal flight is wallpaper).

| Status | Trigger | Sticky |
|---|---|---|
| Cancelled | airline | yes — also hides the phase pill |
| Diverted | airline | yes |
| Delayed | see invariant 4 | no — clears if the airline pulls the time back |

The lateness NOTE ("out 12 min late") is separate, measured against the
FFDO bid line, and shown regardless of the pill. Both can be true.

## AIRCRAFT MATCHING

Callsigns are not unique to a leg: regional turns fly out and back under
one number, and the return departs inside the outbound's window.

Behaviour unchanged since v4 — the most correct part of the app. Storage
moved into columns.

0. **Arbitrate** — of legs sharing a callsign that day, only the latest
   whose scheduled departure has passed may claim the aircraft.
   Deterministic; needs no observation (outstations often lack coverage).
1. **Acquire** — adopt on callsign when at ORIGIN (≤30nm) or within
   T-20/T+45. The window covers no-receiver outstations; safe against
   turns because the return cannot depart until this leg lands.
2. **Hold** — thereafter only that hex, unconditionally, anywhere.
   Diversions and returns-to-field are followed.
3. **Release** — a closed leg accepts nothing further from any source.

## CLOSURE

| `closed_by` | Meaning |
|---|---|
| `airline` | airline gate-in, or cancellation |
| `relaunch` | aircraft took off again. Unambiguous, free |
| `observed` | confirmed landing + 5 min stopped + 8 min silent |
| `backstop` | 3h past revised arrival, quiet, no fresh airline data |

Observed closes the leg **even with an API key** (owner's decision).
`actual_in` is the OOOI field most often missing; v5.0 waited on it and
hung. A late airline gate-in **upgrades** an observed/backstop close.

Both halves of `observed` are required: a plane holding off-gate stays
stationary while transmitting; a coverage hole is silence without a stop.

## WHICH LEG IS CURRENT

Clock first, evidence on top. The window runs T-20 to scheduled arrival
+3h (`CURRENT_GRACE`). Then two overrides, each fixing a failure the clock
alone produced in the opposite direction:

  * `_still_flying()` — holds the card past the grace while the aircraft
    is demonstrably UP and has not come down. Three hours late and still
    at altitude is a normal bad day, and the card used to drop into past
    flights mid-cruise, exactly when the family is watching hardest.
  * ...but NOT once it is down. `landed_seen` / `on_actual_api` /
    `in_actual_api` end the hold even though the leg never closed. Gate-in
    is the OOOI field most often missing entirely, so a leg can sit open
    forever with the aeroplane parked — holding the card on that one is
    what stopped the next flight ever becoming current.
  * `_has_started()` — when several legs qualify at once, one with real
    evidence of having departed beats one that has merely reached its
    scheduled time. Without it a delayed leg 2 took the card off an
    airborne leg 1, because leg 2's window opened first.

`MAX_AIRBORNE_HOLD` (12h) is the ceiling, so a stuck `airborne_seen` flag
cannot own the card indefinitely. A candidate that loses is appended to
`past`, not dropped — it is behind the leg now flying.

## QUICK START

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

## SCHEDULE FORMAT

Paste exactly like this (one leg per line):

```
06/26/2026 3729 DFW 1742 OKC 1837
06/26/2026 3729 OKC 1911 DFW 2011
06/26/2026 3566 DFW 2227 ICT 2351
```

`MM/DD/YYYY  FLIGHT  ORIG  DEPTIME  DEST  ARRTIME`

Times are local block times at each airport. Each row has an "×" button on
`/admin` to delete it individually if needed.

## ACCOUNTS & SHARING

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

## DEPLOY ON TRUENAS WITH DOCKGE

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

## UPDATING

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

## WHEN AEROAPI IS QUERIED

**One rule.** Every leg is handed `TICKETS_PER_LEG` (18) tickets and spends
them like this:

    time left in the window / tickets left = how long to wait

The window runs from 30 minutes before SCHEDULED departure to an hour after
the BEST CURRENTLY KNOWN arrival. That last part is the whole delay story:
when the airline publishes a revised arrival, the window stretches and the
remaining tickets re-space themselves across it automatically. A six-hour
delay widens the gaps instead of draining the budget.

Two clamps hold the edges:

  * `MIN_QUERY_GAP` (5 min) — never faster, so a garbage timestamp can't
    empty the wallet in a single sweep.
  * `MAX_QUERY_GAP` (20 min) — never slower. This is also what covers a
    flight overrunning its window with nothing published: the remaining
    time goes negative, the formula falls through to this, and the leg
    ticks over quietly instead of stopping dead.

`ARRIVAL_RESERVE` (4) of the tickets are locked until the aircraft is
actually down, or its arrival time has passed. Gate-in is the one answer
that ENDS a leg, so it can't be starved by a long delay upstream. The clock
half of that condition matters for legs with no ADS-B coverage, where no
touchdown can ever be observed.

The leg stops spending the moment there's nothing left to learn — gate-in
received, cancelled, or closed. Unspent tickets simply go unspent; there is
no prize for using them.

The background poller has to reach a leg BEFORE it becomes the current
flight, or the first look can never happen: a leg isn't `current` until
T-20, so the poller carries its own `PREVIEW_WINDOW` (35 min) and sweeps
imminent upcoming legs too. That window is deliberately in the poller
rather than in `get_current_info`, because "current" also drives flight
selection, the map and the card, and moving that boundary would change all
of them.

AeroAPI's own `departure_delay` / `arrival_delay` fields are fetched but
deliberately NOT stored. They are measured against the airline's published
schedule; every delay figure in this app is measured against the FFDO bid
line, because that is what the pilot flies. Keeping both would mean two
numbers for one thing and an invitation to trust the wrong one. The full
raw record is kept in `api_raw` regardless, so nothing is lost.

### What this replaced (v5.1 and earlier)

Six independent triggers — first look at T-30, a ground watch at T+20 and
every 30 min after, three evenly spaced cruise checks, wheels-down+5, a
closeout loop, and a no-ADS-B arrival fallback — each with its own cap and
its own counter column. They worked. But the interactions between them were
where the bugs lived, and three cruise checks on a 95-minute regional leg
bought the same answer three times over. The `closeout_tries`,
`fallback_tries` and `delay_watch_tries` columns still exist on the table
(migrations here are append-only) but nothing reads or writes them.

### Measured against a real schedule

Two actual months of FFDO lines, simulated against the shipped
`should_query()` at 20-second poller resolution:

| Scenario | July (26 legs) | August (41 legs) |
|---|---|---|
| Normal, gate-in published | $1.70 | $2.76 |
| Gate-in never published | $2.34 | $3.69 |
| 45-min delay, published | $1.86 | $2.97 |
| 6-hour delay, published | $1.95 | $3.08 |
| 6-hour delay, airline silent | $2.34 | $3.69 |
| No ADS-B coverage at all | $1.70 | $2.76 |

The per-leg ceiling is a hard 18 in every scenario, so a 50-leg month
cannot exceed $4.50 even if every single flight goes wrong.

## COST CONTROL

`/flights/{ident}` costs $0.005 per result set; `/schedules` costs $0.02,
four times as much, which is why deadhead carrier resolution is capped,
counted at four units, and stored permanently once it succeeds.

At 18 tickets per leg, a heavy 50-leg month has a hard ceiling of $4.50,
and real spend lands well under because most legs stop early the moment
gate-in arrives. The per-pilot monthly limit is a hard stop on top of that
— queries cease entirely once it's reached, so the app can never quietly
produce a bill.

That limit is set by each pilot in Settings ("Monthly spend limit"), stored
on the `users` row, and defaults to $4.90 — just under the Personal tier's
$5 free credit. It was $4.50 through v5.1; the v5.2 migration moves any row
still sitting on exactly the old default, and leaves any other value alone
on the grounds that a pilot who typed a number meant it.

### Deadhead carrier resolution

An FFDO line gives a bare flight number, never an airline. For the pilot's
own legs that's fine — they're Envoy. A deadhead is usually on mainline
American or another wholly-owned regional, each broadcasting its own
callsign, so looking up ENY4110 when the aircraft squawks AAL4110 means the
leg never tracks at all.

Resolution order, and the caps on it:

  1. **The free ADS-B probe goes first.** Try the handful of callsigns
     American's family actually uses and see which one has an aircraft
     within 40 nm of the origin around departure. Costs nothing.
  2. **Then, at most twice ever, a paid `/schedules` lookup**, spaced an
     hour apart, recorded on the row in `carrier_tries` / `carrier_tried_at`
     BEFORE the call is made — so a timeout or a crash mid-request still
     counts. It goes through `payer_for()`, so it obeys the same monthly
     cap as everything else.

Through v5.1 a FAILED lookup wrote nothing down. The poller sweeps every 20
seconds and a deadhead sits in its window for five or six hours, so the
identical failing question was asked roughly a thousand times — at $0.02
each, outside the budget check, and invisible to the local counter. One bad
deadhead could spend the entire month in an afternoon with nothing on
screen changing. `tests_carrier_cap.py` drives 900 sweeps and asserts at
most two paid lookups.

### FFDO placeholder lines

An FFDO block carries non-flying lines that fit the same shape as a leg —
`07/05/2026 0 DFW 1946 DFW 1946` is a duty or hotel marker. Same airport
both ends, flight number zero. Through v5.1 the parser accepted them, so
each became a tracked "flight" that looked up a callsign nobody broadcasts
and spent its ticket allowance discovering that. They're dropped in
`parser.py` now, before they reach the schedule, the poller or the card.

The cap is enforced against FlightAware's own usage figure, which is
refreshed every 15 minutes (`USAGE_REFRESH`). That endpoint is free, and
the one number that must never be stale is the one deciding whether to stop
spending. A reading older than an hour is treated as a FLOOR rather than
the truth, and the local count takes over.
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

## NOTES

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

## STORAGE & MIGRATION

Everything in `data/flighttracker.db` (SQLite, WAL). `data/secret_key.txt`
signs session cookies — generated once, must stay stable, never packaged.

Migration runs on boot, is **idempotent**, and handles two source shapes:

| From | Path |
|---|---|
| v4 (7 tables) | `legs` → `flights` + `roster`; `flight_tracks` → `positions`; dead `aircraft` and old user-scoped `positions` dropped |
| v5.0 (per-user `flights`) | renamed `flights_v50`, merged to one shared row per flight; observed/airline columns copied field-by-field (non-null wins) |

**Carries over:** accounts, settings, schedule, all flown tracks, and (from
v5.0 only) observed and airline data.

**Does NOT carry over from v4:** enrichment and closeout JSON blobs. ≤30
days old, re-fetchable, and mapping two nested documents into 80 columns is
a one-off guess. Symptom: past flights show route and path but no gate
times until re-flown.

**Not dropped:** v4 tables holding real data (`legs`, `flight_tracks`,
`flight_aircraft`, `flight_enrichment`, `flight_closeout`) and
`flights_v50` are left in place for recovery. Drop by hand once satisfied.

First-boot log lines to expect: `dropped the dead v4 positions table`,
`carried N track points over from v4`, `carried N schedule legs over from
v4`, or `merged N per-user v5.0 rows into shared flights`.

## TESTS

```bash
python tests_flight_row.py          # 50
python tests_poller_end_to_end.py   # 27
python tests_past_leg_detail.py     # 19
python tests_budget_limit.py        # 17
```

Each uses its own scratch DB via `PT_DB_FILE`. Read
`tests_poller_end_to_end.py` first: it scripts an ADS-B feed and walks one
leg through pushback → taxi → climb → cruise → **total loss of coverage** →
re-acquisition → approach → landing → taxi-in → block-in, asserting both
pills and the closure decision at each step. The coverage-gap step is
invariant 1.

**Fixture traps, each of which cost time:**
- `dep_time_local` is local to the ORIGIN. Building it from UTC hands puts
  a PHX leg 7h out, silently outside the query window.
- A leg needs a ROW in `flights` before `refresh()` does anything; it reads
  counters from there. A bare Python object is correctly declined.
- `poller` does `from .livesource import live_state` — binds at import.
  Patch `poller.live_state`, not `livesource.live_state`.
- Usernames must be ≥3 chars; `create_user` rejects shorter silently from
  the HTTP layer's perspective (form redisplay, HTTP 200).

## VERSION HISTORY

### v5.5 - live rows, live clocks

- **The live flight's list row went stale.** Rows are server-rendered once
  at page load and only the CARD was repainted by the poll, so a page
  opened before pushback still read "Scheduled" an hour into the cruise
  while the card above it said "In air". `updateRowTags()` now repaints the
  live row's pills each poll — pills only, so an open detail panel beneath
  survives.
- **"23 min ago" is computed on the page, not baked into the HTML.**
  `_ago_text` ran once server-side and the result then sat there getting
  quietly wronger until a reload. `view.build` now also emits
  `enriched_at_iso` / `last_signal_iso`, and `tickRelativeTimes()` rewrites
  every `[data-ago]` element every 30s. The JS formatter mirrors
  `_ago_text` exactly so server-rendered and client-recomputed values can
  never disagree.
- **Three detail rows became two.** Departure / Arrival / Scheduled split
  one fact across three lines and still left arithmetic to do — the note
  said "28 min late" on one row while the time it was late relative to sat
  two rows below. `_time_line()` now builds one self-contained cell per
  row: revised time, then "28 min late - was 12:11 CDT" in small print
  under it. An unflown leg still shows its scheduled times, which the
  deleted Scheduled row used to cover.
- **Bigger disclosure caret** on list rows (0.75rem -> 1.25rem).


### v5.4 - the flight list

- **One list instead of two.** Past, the live flight and upcoming were
  rendered through separate `group_legs_by_day` calls with the current leg
  in neither, so the list had a hole exactly where the pilot is. Now built
  once by `build_flight_list()`, chronological, with the live flight IN it
  and marked. A day holding both a flown leg and the live one no longer
  produces two cards with the same label.
- **Tapping a flight expands it in place.** It no longer swaps the card and
  map, which meant looking up last Tuesday's arrival gate cost you sight of
  the live flight. Detail comes from the existing `/api/leg/{id}` on first
  open, one fetch per row, cached. Entirely read-only: opening every past
  flight in the month costs no AeroAPI queries.
- **Past flights keep all their data.** Gates, baggage claim, aircraft,
  actual times and closeout source were always in the row and always
  returned by `view.build` — nothing was ever discarded. They simply had
  nowhere to render. The wife-collecting-the-pilot case works on a leg that
  landed yesterday.
- **Empty fields are never drawn.** An unflown leg shows only what exists,
  rather than a column of blanks that reads as broken.
- **Past visibility is a body class,** not a wrapper. Past and live rows now
  interleave inside one list, and a single collapsible container cannot
  express "hide three rows in this day but keep the fourth".


### v5.3 - UI fixes

- **Layovers straddling the past/upcoming split were invisible.** The
  tracker renders those two lists through separate `group_legs_by_day`
  calls, so a layover with its arrival in one and its departure in the
  other had a bucket on each side and a neighbour on neither. Now computed
  once over the whole schedule by `overnight_index()`. Multi-night gaps
  read "2 nights in X" rather than "Overnight in X", and layovers are
  bounded at both ends (3h floor, 35h ceiling) so a midnight-crossing turn
  isn't an overnight and days off between trips aren't a layover.
- **`/account/usage` only refreshed while a leg was active.** It lived
  inside `_settle`, which runs per active leg, so on a day off nothing
  asked and the reading went 20+ hours stale — and `budget_state` falls
  back to the local count once a reading is stale, meaning the number the
  cap is enforced against decayed exactly when nothing was refreshing it.
  Moved to `poll_once`, all users, every sweep.
- **Settings usage figure overlapped the text below it.** `.usage-line` was
  a space-between flex row that wraps on a phone; `.hint`'s negative top
  margin then climbed over the wrapped line. Rebuilt as a stacked block.
  Now also shows when the tracker last swept, separately from the spend
  reading's age — one figure could not distinguish "usage endpoint
  unhappy" from "poller stopped".
- **Past legs claimed to be "Scheduled".** `leg_view` falls back to
  Scheduled with no stored phase, and nothing sweeps a leg once it is past,
  so a leg imported after it was flown would read Scheduled forever. Reads
  "Not tracked" past `UNTRACKED_AFTER`.
- **Flight sequencing conflated two opposite failures.** Purely clock-based
  selection dropped a leg 3h past SCHEDULED arrival, so a still-airborne
  leg fell into past flights mid-cruise, while a landed-but-never-closed
  leg held the card indefinitely. The dividing line is airborne vs. on the
  ground: `_still_flying` holds the card only while genuinely up, and
  `_has_started` means a leg that has actually departed beats one that has
  merely reached its scheduled time. 12h ceiling on the hold.
- **FFDO placeholder rows imported before v5.2 are purged on boot.** The
  parser filter only ever helped future imports.
- **Calendar DH badge moved after the route**, where it no longer shoves
  every deadhead's origin/destination right by its own width.
- **Past-flights toggle no longer jumps the page.** The list expands above
  the upcoming list, so opening it shoved everything down while the browser
  held scrollTop. `togglePast()` now measures `#past-anchor` before and
  after and scrolls by the delta.
- `tests_ui_fixes.py` added (21 assertions).

### v5.2 - one polling rule instead of six

- **Six AeroAPI triggers replaced by the ticket rule.** 18 tickets per leg,
  spaced by "time left in the window / tickets left", clamped to 5-20 min,
  4 held back for arrival. Delays are handled by the window stretching, not
  by a dedicated watcher. Measured at $1.70-$3.69/month across two real
  months of FFDO lines; hard ceiling $4.50 at 50 legs.
- **Deadhead carrier lookup no longer runs away.** Free ADS-B probe first,
  then at most two paid `/schedules` calls per leg ever, recorded before the
  call is made, counted at their real $0.02 price, and under the budget cap.
  Was ~1,000 uncapped, uncounted, un-budgeted calls on a single bad leg.
- **FFDO placeholder lines dropped in the parser** (same airport both ends,
  or flight number zero) instead of becoming tracked flights.
- **Usage refresh 1h -> 15 min**, stale threshold 3h -> 1h. The endpoint is
  free, and the cap is only as good as the number it reads.
- **Default monthly cap $4.50 -> $4.90**, with a migration that moves rows
  sitting on exactly the old default and leaves chosen values alone.
- `tests_carrier_cap.py` added (13 assertions).

### v5.1 — shared flights

Owner confirmed FOs are using the app and flying the same legs. v5.0 gave
each pilot a private row for a shared aeroplane.

- `flights` is now keyed by flight id alone and **shared by all crew**.
  New `roster` table holds per-person facts (`sort_index`, `is_deadhead`,
  `trip_start`). Four tables total.
- **One AeroAPI query per flight, not per pilot.** `enrichment.payer_for()`
  picks the lowest user id with a key and remaining budget; falls through
  to the next if capped. `flights.api_paid_by` records who paid.
- Importing a leg another pilot already has **adopts** the existing row —
  the joining pilot immediately sees everything observed or paid for.
- Schedule fields are written only when a flight is NEW, so two bid lines
  cannot fight over one row.
- Deleting a leg or an account removes `roster` entries only; shared
  flights and tracks survive. Orphans swept by `purge_old()`.
- `write_all_owners` / `get_row(user_id, ...)` / `get_row_any` removed
  rather than aliased, so a future session cannot write per-user code
  against a shared table.
- Fixed: `check_aeroapi.py` imported three functions deleted in v5.0 and
  crashed on startup — the exact tool needed when AeroAPI looks wrong.
- Fixed: "waiting on airline gate-in" was computed but never rendered; now
  shares the small-print slot with the signal note.
- Removed dead code: `tags.never_tracked`, unused imports in `view.py`.
- AeroAPI's own `departure_delay`/`arrival_delay` are fetched but
  deliberately NOT stored — they measure against the airline's published
  schedule, while every delay figure here measures against the FFDO bid
  line. Two numbers for one thing invites trusting the wrong one. Full raw
  record kept in `api_raw`.

### v5.0 — the data rebuild

Owner's read on v4: ADS-B and AeroAPI had been "glued together", and phase
tags were often wrong. Both correct.

- Seven tables → three. `legs` + `flight_aircraft` + `flight_enrichment` +
  `flight_closeout` collapsed into one row per leg with named columns.
  `aircraft` and old `positions` were dead; dropped.
- Reconciliation moved from DISPLAY time to WRITE time. The page stopped
  writing to the database (invariant 2).
- One badge → two pills.

Bugs fixed:
- Phase fell backwards on a coverage gap ("In air" → "Unknown" mid-cruise).
- "Delayed" fired on observed lateness; a 12-min pushback lit the pill.
- Backstop could close a delayed flight before it departed *(owner-found)*.
- Observed arrival could be read off an aircraft that never moved
  *(owner-found)*.
- Closeout hung with an API key — only `actual_in` could close a leg.
- `flight_closeout` declared twice in `db.py` with two different shapes.
- Re-pasting a schedule wiped observed data (`save_schedule` deleted first).
- `Landing` unreachable without an API key: SQLite `0` vs `is False`
  *(found by the new test suite)*.
- Poller used two clocks in one sweep.
- v4→v5 migration hit a `positions` name collision — invisible on a fresh
  install, only on upgrade.

Docs drift corrected: query floor 15→20 min, closeout tries 3→2.
Tests 33 → 106.
