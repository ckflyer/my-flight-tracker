# MyPilot

Self-hosted flight tracking for airline crew and their families. FastAPI +
SQLite + Jinja, deployed via Docker on TrueNAS/Dockge. Version 1.14.1.

The version above was stale at 1.4.0 for five releases. `app/version.py`
is the only authority; this line is a convenience and nothing reads it.

Formerly "flight-tracker" / "Pilot Tracker". Renamed in 1.0.0; see VERSION
HISTORY for why the version number restarted.

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
1. Bump `app/version.py`. It is SEMVER now (MAJOR.MINOR.PATCH) — pick the
   field that matches the size of the change, and read the docstring in that
   file before guessing. VERSION also keys the service worker cache, so
   skipping the bump means phones keep serving the old build.
2. Add a `## VERSION HISTORY` entry at the top of it.
3. Update `## STATE` and `## OPEN`.
4. Record any bug you hit and how it was diagnosed.
5. Run all EIGHT test suites. Package only if all pass.
6. Never ship `data/*.db` or `data/secret_key.txt`. Check the packaged
   zip, not just the working tree — a test run recreates both.

**Owner context:** no programming background. Explain reasoning in prose in
chat, not jargon. He is a line pilot and is the authority on operational
questions (what "delayed" means, when a flight is over) — ask him rather
than inferring. He has caught two real bugs by inspection; take his hunches
seriously.

**Deploy workflow:** he drops extracted files into a GitHub repo, runs
`git pull` on TrueNAS, then `update.sh` via Dockge.

## STATE

**v1.14.1.** Renamed to MyPilot in 1.0.0. Deployed target: TrueNAS. Multi-user: the
owner plus several FOs, who fly the same legs — hence shared flight rows
(v5.1, retained).

The v6.4–v7.4 documentation gap is CLOSED as far as inspection can close it:
what those versions built has been verified against the tree and folded into
the sections below. Their RATIONALE is still unrecorded and unrecoverable —
if you are about to change the shared stylesheet, the tab bar, or the light
theme, read the code, because this file cannot tell you why they are the way
they are.

**Current work: see `## NEXT UP`.** N1 is DONE (1.5.0). N2 (logbook) is
next and is now unblocked: flights accumulate, the roster is month-filtered
and chronologically ordered, `in_actual_api` is chased rather than lost, and
simulated legs are flagged so the logbook has something to exclude.

Not on any list, done along the way: test mode, a second admin, and the
page split (1.6.0–1.7.0). All three came out of the same problem — the app
could not be OPERATED without SSH, and bugs could not be reproduced without
flying a trip.

Tests: **1,140**, eleven suites, all passing.

**Current work: the UI chunks (1.9.0 onward).** Five agreed steps, owner's
brief, reworking the tracker and calendar around one flight-strip
component modelled on a reference consumer app. This is NOT a detour
around NEXT UP — N2 (logbook) still follows — but the tracker had grown
three different ways of drawing the same thing and the calendar had to
become the history browser before past flights could leave the tracker.

| Step | What | State |
|---|---|---|
| 1 | the `.fstrip` component + the current flight card | **DONE 1.9.0** |
| 2 | the expanded view, on the reference layout | **DONE 1.10.0-1.10.2** |
| 3 | tracker list: current trip only, no past-flights toggle, positioned on the live leg | **DONE 1.11.0 + 1.12.0** |
| 3b | the row dropdown onto `.aptblock` (scroll position solved by the sheet) | after 4 |
| 4 | calendar: expandable strips with history and a mini map | next |
| 5 | regression pass across themes, time formats and the odd states | |

Step 3 carries a DECIDED behaviour worth not re-litigating: once a trip
ends, the first leg of the NEXT trip takes the card, so the question "when
do I leave again" is answered without navigating. And past flights leave
the tracker entirely — they belong to step 4's calendar.

| Suite | N | Covers |
|---|---|---|
| `tests_flight_row.py` | 68 | write modes, both tag ladders, closure guards, shared crew, retention |
| `tests_poller_end_to_end.py` | 47 | full flight gate-to-gate, scripted ADS-B feed |
| `tests_past_leg_detail.py` | 19 | past-leg + T-30 preview rendering |
| `tests_budget_limit.py` | 17 | monthly spend cap at its enforcement point |
| `tests_carrier_cap.py` | 13 | deadhead lookup cap, placeholder filter |
| `tests_ui_fixes.py` | 527 | the flight strip staying ONE component, layover labels, untracked phase, sequencing, flight list, time lines, viewer.html template audit, import diff page, month filter, calendar month nav |
| `tests_app_shell.py` | 167 | install shell on every page, service worker, manifest, icon styles, version ordering, schema guard, rebrand |
| `tests_timezones.py` | 68 | DST both directions, arrival-date resolution, date line, stored-timestamp parsing |
| `tests_closeout_sweep.py` | 42 | the abandonment cliff, the on-ground handover, the late gate-in chase and its cap |
| `tests_import_merge.py` | 39 | additive import, month scoping, future-only reconciliation, the diff, manual add |
| `tests_test_mode.py` | 133 | simulator isolation (no spend, no ADS-B, no logbook), each scenario, admin promotion + password gate, the one-aeroplane rule |

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
### CLOSED since this list was last edited ✅

Verified in the v7.4 tree, listed here so nobody "fixes" them twice:

- **One shared palette — DONE.** `static/app.css` now holds the only copy
  and all ten templates link it. Its header comment documents the
  `data-theme` vs `prefers-color-scheme` precedence rule; the
  `:not([data-theme])` guard on the media query is load-bearing and
  explained there.
- **Light theme on the auth pages — DONE.** Handled by the same file.
- **Bottom tab bar — DONE.** `<nav class="tabbar">` on all four logged-in
  pages (viewer, calendar, admin, settings), pilot-only entries gated on
  `is_pilot`. See the caveat in ROADMAP P0-4: the links are plain `<a
  href>`, so every tap is still a full page load.

- **Service worker — DONE (1.0.0).** `static/sw.js`, served from `/sw.js` so
  its scope is the whole origin. Cache name keyed to VERSION.
- **Manifest and theme-color on every page — DONE (1.0.0).** Via
  `templates/partials/app_shell.html`, included by all ten templates and
  enforced by `tests_app_shell.py`. The manifest is now a ROUTE
  (`/manifest.webmanifest`), generated per user so the icon choice applies.
- **`theme_color` mismatch — DONE (1.0.0).** Now `#0f1419`, matching `--bg`.
- **Carrier trademarks — DONE (1.0.0).** Renamed to MyPilot throughout;
  callsign prefixes moved to `app/carriers.py` as configuration. A regression
  guard scans templates and static files for the old names.
- **API versioning — DONE (1.0.0).** `/api/v1/…` with the bare paths kept as
  aliases.
- **Retention — DONE (1.0.0).** 30 days → 365, in `flights.py` and
  `track.py` together, `PT_RETENTION_DAYS` overridable. See BACKUP.md: this
  is the release where the database stopped being disposable.
- **Offline/connection state — DONE (1.0.0).** Three distinguished states,
  driven by real poll outcomes rather than `navigator.onLine`.

### STILL OPEN — verified against the v1.0.0 tree ✅

- **1,376 of viewer.html's 2,333 lines are inline `<script>`.** This is the
  mechanism behind the recurring "viewer.html silently loses JavaScript"
  failure documented in NOTES — layout edits and logic edits collide
  because they live in one file. See ROADMAP P0-6.
- **Cookie-auth only** — no bearer tokens. See MIGRATION AND FUTURE-PROOFING.
- **API returns presentation, not facts.** `/api/selected` emits
  `dep_line`, `arr_line`, `dep_shown`, `ete`, `status` as pre-formatted
  display strings, and takes `time_format` ("12"/"24") as an argument so
  the SERVER does the formatting. Only `enriched_at_iso` and
  `last_signal_iso` send a machine-readable value. Any non-browser client
  is blocked on this. See ROADMAP P1-1.
- **Schedule import has only ever been fed one carrier's FFDO lines.** This is the
  gate on every other person using the app, and it is not a UI problem.
  See ROADMAP P0-7.
- **Import REPLACES the roster rather than adding to it.** Pasting a new
  month deletes roster rows for legs not in the paste, including past ones.
  Flight rows survive (shared, adopted); only the link is pruned. This is
  the single blocker on the app being a record rather than a rolling
  window. Owned by NEXT UP / N1.
- **One share code per pilot.** No named invites, no per-person
  revocation, no expiry, no last-seen. Owned by NEXT UP / N4.
- **No self-service account deletion.** `settings.html` has admin-deletes-
  a-user only (`/settings/users/delete/{user_id}`). Apple requires an
  in-app deletion path for any app offering account creation, and it is
  correct regardless. See ROADMAP P0-9.
- **AeroAPI Personal tier is personal-use only; community ADS-B feeds
  (adsb.lol, adsb.fi) are non-commercial.** airplanes.live already
  withdrew access. Neither permits charging money. This is a hard legal
  gate in front of any paid tier, not a detail. See ROADMAP T2.

## NEXT UP — the agreed build order

These five are COMMITTED, in this order, and the order is a dependency
chain rather than a preference. Everything after N1 assumes flights
accumulate instead of rolling over, so building any of N2–N5 first means
building it twice.

This section is the working plan. P0/P1 below remain the standing backlog.

---

### N1 — additive import + manual remove ✅ DONE in 1.5.0

**The problem.** `save_schedule` currently REPLACES a pilot's roster: any
leg not present in the new paste has its roster row deleted. Pasting
September therefore erases August from that pilot's view. Combined with the
old 30-day retention this made the app a rolling window, which is exactly
what a logbook cannot be.

Note what is NOT broken: flight ROWS are shared and adopted, never
duplicated (v5.1). Only the roster LINK is pruned. So this is a small
change, not a rewrite.

**The change.**
- Prune only FUTURE roster entries. A re-paste is the pilot correcting what
  is COMING; a leg that already departed happened, and an import must not
  be able to revise history.
- Import runs by MONTH. The paste declares which month it covers, and
  reconciliation is scoped to that month — so importing September cannot
  touch August even for future-dated legs.
- Nothing is applied silently. `import_review.html` already exists; it
  becomes a DIFF the pilot approves: added / removed / changed / unchanged.
- Manual per-leg remove, and manual per-leg ADD. The add path is what
  covers a diversion that continued to the original destination — a leg
  that never existed in any bid line and never will.

**Why a diff rather than a silent merge.** Two failures need catching and
neither announces itself: a trip dropped from the line that the pilot
forgot to remove, and a leg flown that was never on the line. The diff is
the only point where a human can see both.

**Decided (owner, 1.5.0):** a removed PAST leg is DELETED OUTRIGHT. The
archive idea was rejected as a state nobody would ever look at — it buys a
distinction ("did not fly" vs "never imported") that costs a column, a
filter on every query that reads the roster, and a second meaning of
"removed" in the UI. An import can never remove a past leg anyway, so the
only way to reach this is a human deliberately deleting one.

**As built.** `save_schedule` is renamed `replace_schedule` and is OFF the
import path entirely — the rename is the fix, because the old name read as
"save this" while the behaviour was "make the roster exactly this". Two new
primitives replace it there: `merge_schedule` (add, remove nothing) and
`remove_legs` (targeted, explicit). Both re-sequence the whole roster into
departure order afterwards, because `sort_index` used to be the leg's
position in the paste, which only works while the paste IS the whole
roster. `app/importer.py` owns the diff and nothing else; the two scope
rules live there with the reasoning attached.

Also shipped alongside, because N1 is what makes them necessary — before
this the roster could not exceed about a month:
- **Month filter on the flights page.** Server-side, `?month=YYYY-MM`, with
  a per-month count and an all-months option.
- **Calendar shows ONE month at a time**, with prev/next and a picker. It
  used to render every month that had data, stacked down one page; at
  365-day retention that is a year of grids in one document.
- **Per-leg drop on the review page**, on the same page as the trip breaks.
  Dropping disables the row's inputs rather than deleting the row, so the
  leg stays visible, struck through, and the choice is reversible.

---

### N2 — logbook view

**Why it needs its own surface.** ~46 legs/month is ~550/year and ~2,750 in
five years. A flat list dies well before that. The tracker must stay small
FOREVER — its whole job is a family member opening it and instantly seeing
where the pilot is. It shows current trip and next trip, and never becomes
a history browser.

**Blocked on nothing as of 1.7.0.** N1 gave it accumulation; the closeout
sweep and gate-in chase gave it actual times worth exporting; `simulated`
gives it the one flag it must filter on. See invariant 20 — a rehearsal in
a logbook would be a fiction in a legal record.

**Three views, ONE database.** (An earlier version of this discussion said
"separate databases" — that was a misreading and would break the shared
flight row design that keeps one AeroAPI query serving every crew member on
a leg. One database, three ways of looking at it.)

| View | Job | Scope |
|---|---|---|
| Tracker | where are they right now | current + next trip |
| Calendar | browse by month | one month |
| **Logbook** | the archive | everything, paged |

**The change.** New pilot-only page. Paged (never render 2,750 rows),
grouped by month, searchable by date range, tail, airport and flight
number. Columns are the logbook fields: date, flight number, tail,
origin/destination, OOOI, block, flight time.

---

### N3 — CSV export

**Everything actual, from the API.** Owner's call, and the right one. Each
OOOI event is stored twice — `*_actual_api` (the airline's figure) and
`*_observed` (what we watched). The export uses the API value. Where it is
absent the row is INCOMPLETE, not filled in from observation: a logbook is
a legal record and a derived time must never masquerade as a reported one.
Carry a source column anyway so the distinction survives the export.

**Deadheads excluded.** `roster.is_deadhead` already knows. You do not log
a deadhead as flight time.

**Leg verification before export.** The owner's refinement, and better than
what was originally proposed. Legs must be CONFIRMED as flown before they
can be exported. This is the mechanism that makes the diversion problem
tractable: rather than the app inferring what happened, the pilot ticks off
what actually did. It also bounds the AeroAPI backfill — only confirmed
legs need their missing OOOI values fetched, so reconciliation cannot run
away with the budget.

**Format.** A plain spreadsheet. Good logbook apps accept an arbitrary CSV
and let the user map columns, so a clean, well-named sheet beats guessing
at one vendor's schema. Both UTC and local for every time, block AND flight
time as separate columns.

**Depends on 1.1.0/1.2.0.** These exports are built from resolved instants.
Shipping this before the timezone work would have written hour-wrong OOOI
times into a real pilot's logbook twice a year.

---

### N4 — per-viewer named invites

**The problem.** One share code per pilot means the family is one
undifferentiated blob. Revocation is all-or-nothing: cutting off one person
logs out the spouse, the parents and every FO simultaneously. And a code is
a bearer secret — whoever holds the text has a live position feed.

**Why not require viewer accounts.** It would fix revocation and cost
adoption. The person who most needs this app is the least likely to create
an account and choose a password. Named invites get most of the security
for none of the signup friction.

**The change.** Codes move to their own table, one row per invite: name,
code, created, last seen, optional expiry, revoked flag. Settings gets a
panel listing active invites, with an add button opening a dialog for name
and expiry, plus per-invite regenerate and revoke.

Same code-generation logic as today. **New constraint: a new code may not
collide with any other ACTIVE code**, across all pilots — two households
must never share a code, and the check has to be against live codes rather
than merely unique-per-pilot.

**Also:** a global pause-sharing switch. Crew on days off may not want a
live feed running at all.

---

### N5 — viewer-side framing

**The insight.** A family member is not asking "where is the aircraft".
They are asking **"when are you home"**. The tracker answers the first
question well and the second one barely.

**The change.**
- Arrival time in the VIEWER's timezone, not the destination airport's.
  Someone in Dallas waiting on a pilot landing in Phoenix wants to know
  when to leave for the airport in their own clock.
- Trip-level framing: "away until Thursday · 2 legs left".
- Surface the pickup details already stored — gate, terminal, baggage.
- A landed-safe history: the last few arrivals, with times.

**Template split happens here.** Pilot and viewer currently share
`viewer.html` (2,333 lines) with `is_pilot` toggles. The owner's proposed
trigger — split on whether the visitor is signed in as a pilot — is the
right line, and N2 plus N5 make it unavoidable. Do the split as part of
N5, not before: splitting an unchanged file achieves nothing, and doing it
alongside a behaviour change makes a regression impossible to attribute.

**Sequencing note:** P0-6 (moving viewer.html's inline JavaScript to
`/static/app.js`) should land BETWEEN N4 and N5. Splitting a template that
still carries 1,376 lines of inline script means splitting the script too,
by hand, which is precisely how the documented "viewer.html silently loses
JavaScript" bug happens.

---

### What this sequence deliberately does NOT do

- **No native client work.** The on-ramp (P1) is being laid as normal
  iteration; the client itself waits for trigger T2.5.
- **No push notifications.** P1-6 records the events; delivery needs a
  host that is always up (T1) and a store presence (T2.5).
- **No payment or hosting work.** Those are trigger-gated below and none of
  the triggers have fired.

---

## ROADMAP

Ordered by cost, not by appeal. Phases P0/P1 are free and are done in
evenings. Everything from T1 down is gated behind a **trigger** — a
condition that must be TRUE before the money is spent. The triggers are
the point of this section. Spending ahead of one is how a hobby becomes an
expensive hobby.

**Governing rule: never do work that only pays off in a future that might
not arrive.** Every P1 item below is dual-purpose — it improves the web
app today AND is the on-ramp to a native client. If a proposed task fails
that test, it is premature.

### P0 — free, small, do first

**Items 1, 2, 3 and 5 shipped in 1.0.0.** Item 4 is partly done: the tab bar
existed already (built in the undocumented v6.x–v7.x range), but its links
are still plain `<a href>`, so every tap is a full page load. That remains
the clearest "this is a website" tell.

Ordered by perceived-quality gain per hour spent.

1. ~~**Service worker.**~~ **DONE 1.0.0.**
2. ~~**Manifest + `theme-color` on all ten templates.**~~ **DONE 1.0.0.**
3. ~~**`theme_color` → `#0f1419`.**~~ **DONE 1.0.0.**
4. **Tab bar taps should not reload the page.** The `<nav class="tabbar">`
   already exists and looks right; each `<a href>` is a full navigation,
   which flashes and resets scroll. Swap content in place using the
   existing `fetch()` pattern, or adopt htmx (~14KB, designed for
   server-rendered apps, leaves the Jinja templates intact). This is the
   remaining "it's a website" tell.
5. ~~**Rename off carrier trademarks.**~~ **DONE 1.0.0** — MyPilot.
6. **Move viewer.html's 1,376 script lines to `/static/app.js`.** No
   behaviour change. Kills the collision class of bug documented in NOTES.
   Keep `test_template_contract` pointed at whatever the new arrangement
   is.
7. **Schedule import for another carrier's bid line.** The real gate. Until
   an FO at another airline can paste a pairing and have it parse, this is
   the owner's app rather than a product. Partly unblocked in 1.0.0:
   callsign prefixes are now configuration (`PT_HOME_CALLSIGN`), so what
   remains is the PARSER, not the carrier assumptions around it.
8. **First-run onboarding for a non-technical viewer.** The person who
   most needs this app is the least equipped to set it up.
9. **Self-service account deletion.** See OPEN.

### P1 — free, and also the native on-ramp

Do these as normal iteration, not as a project.

1. **Send facts alongside the formatted strings.** For every time value
   emitted, add an ISO-8601 UTC field and the airport's IANA zone name,
   the way `enriched_at_iso` already does. Keep the pretty string.
   *Today:* the 12/24 toggle stops needing a round trip and the
   `pt_viewer_tf` cookie workaround can retire. *Later:* this is the only
   thing a non-browser client can consume.
2. **Version the API** — `/api/v1/…`. Web clients always have the newest
   build; app clients do not. Without a version, the day a field changes
   is the day an installed build breaks.
3. **Bearer-token auth alongside cookies.** Cookies are fine in a browser
   and inside a WebView. Native clients want a token, and push
   registration needs one. `auth.py` is 10KB now; it will not stay that
   way.
4. **API-first habit.** New feature ⇒ JSON endpoint first, page consumes
   it second. Never build a feature that exists only inside a template.
5. **Keep the client dumb.** In the JS, sort code that DECIDES from code
   that DISPLAYS, and push decisions serverward as they surface. The v4→v5
   move already did this for status and closure — that work is why a
   native client would inherit the app's intelligence for free. Do not
   regress it.
6. **Record notification events.** The poller already knows wheels-down,
   gate-in, delay-published, diversion. Write them to a small table when
   they occur. *Today:* an audit trail and an in-app feed. *Later:* that
   table IS the push queue, and none of the logic is client-specific.

### T1 — trigger: P0 done AND ~10 crew families want in

**Spend: ~$25/mo + ~$15/yr.** Cloud VPS + domain; move off the NAS.
Reason is not performance (FastAPI + SQLite is light) — it is that
strangers should not be reaching the owner's home network, and push later
requires a host that is always up.

`docker-compose.yml` ports over nearly unchanged. Back up
`data/flighttracker.db` first; see STORAGE & MIGRATION.

### T1.5 — trigger: retention is now a year, so backups are mandatory

**Spend: nothing.** Set up the schedule in BACKUP.md. This moved up the list
in 1.0.0: the database went from a disposable 30-day window to the only copy
of a year of flying, in one release. Do this before T2, not after.

### T2 — trigger: ~30 people have said, by name, that they would pay

**Spend: ~$150/mo.** AeroAPI **Standard** ($100/mo minimum, and the
minimum is a floor that usage counts toward, not a surcharge), LLC,
Stripe, privacy policy, terms.

Measured spend is ~$2.76/leg-month per pilot, worst case $3.69, hard
ceiling $4.50 at 18 tickets/leg. So the crossover is ≈36 paying crew:
below that the minimum dominates; above it, marginal cost per pilot stays
under $3/mo indefinitely.

**Blocking sub-task: settle the ADS-B licence before taking a dollar.**
adsb.lol and adsb.fi are non-commercial community feeds — the same
category as airplanes.live, which already revoked access. Either obtain
commercial terms or fall back to AeroAPI-only for the paid tier.

**Also at T2:** BYOAPI disappears. One Standard key, held centrally. The
existing per-user spend cap becomes a fair-use limit rather than a billing
guard — same code, different meaning.

### T2.5 — trigger: hosted centrally, and people want it on their phones

**Spend: $99/yr (Apple) and/or $25 once (Google).**

- **TestFlight** takes up to 10,000 external testers by public link. Beta
  App Review is a lighter gate than full App Store review, so real push on
  real crew phones arrives well before the minimum-functionality argument
  has to be won. Builds expire every 90 days — re-upload is permanent
  homework. No real in-app purchases (sandbox only); bill via the web.
- **Google Play internal testing** holds 100 testers with no closed-test
  requirement. Cheapest possible real-app experiment.

Shell = Capacitor around the existing web app. Server-rendered UI means
`update.sh` still updates the app's content for everyone instantly; only
the shell itself needs store review.

### T3 — trigger: revenue covers costs, two months running

**Spend: as T2.5 plus a contractor for push plumbing.**

Public store listings. **Do not submit a bare wrapper.** Apple's guideline
4.2 rejects wrapped sites with no native function, and a second submission
of the same thing with a new icon does not pass. Ship with: push tied to
real events, a lock-screen Live Activity for the leg in progress, offline
state, and Face ID. Those are not decoration — they are the reason the
shell exists, and P1-6 already built the server half.

Android production note: personal Play accounts created after 2023-11-13
must run a closed test with ≥12 testers opted in for 14 continuous days.
Trivial with a crew base; an organisation account (LLC + D-U-N-S) is
exempt but verification takes weeks.

### T4 — trigger: ~300 paying users AND the owner is the bottleneck

**Spend: equity, not cash.** Bring in an engineer — ideally a pilot who
codes, since the domain transfer is the expensive part and they arrive
with it. Vest over four years. Never trade equity for a promise.

### Explicitly NOT on this roadmap

- **A Flutter/React Native rewrite.** Backend survives; all ten templates
  do not. Same cost as a second product, for benefits that do not apply
  at this size, and it forfeits instant deploys.
- **Removing server rendering.** It is why `update.sh` updates everyone at
  once. That property is load-bearing for a one-person project.
- **A native iOS build against a free Personal Team.** 7-day profile
  expiry, 3-app cap — and personal teams cannot sign the push entitlement
  at all, so it cannot test the only feature that justifies the shell.

## VERSIONING

Four numbers, each answering a different question. Full rationale lives in
`app/version.py`; this is the map.

| Number | Answers | Today | Moves when |
|---|---|---|---|
| `VERSION` | which BUILD is running | `1.0.0` | every release |
| `API_VERSION` | what SHAPE the JSON is | `1` | a field is removed, renamed or retyped |
| `SCHEMA_VERSION` | what SHAPE the database is | `1` | a migration changes table structure |
| `MIN_CLIENT_VERSION` | oldest BUILD still allowed | `1.0.0` | an old client genuinely cannot work |

**Why the decimal scheme was abandoned.** Versions used to be 5.5, 6.3, 7.4.
After 7.9 comes 7.10, and `"7.10" < "7.9"` as text while `7.10 == 7.1` as a
number. Anything comparing versions reads the newer build as older, and no
later patch can fix it because the numbers themselves are ambiguous. Semver
compares field by field as integers. Use `version_tuple()`; never compare
version strings directly and never cast one to a float.

**Which field to bump.** MAJOR breaks something — data migrates one way, or
an old client stops working; back up first. MINOR adds a capability without
breaking anything; the common case. PATCH fixes a bug and adds nothing.

**VERSION is not cosmetic any more.** It keys the service worker cache name
(`static/sw.js`). Ship without bumping it and every installed phone keeps
serving the previous build's CSS and JavaScript — a failure that looks
exactly like "the server didn't update", and the worst thing on this list to
debug.

## MIGRATION AND FUTURE-PROOFING

Everything here is about clients and databases this build **cannot reach**.
That set is empty today and stops being empty the first time an app is
installed from a store. All of it was cheap to add now and expensive-to-
impossible to retrofit later, which is the only reason it exists this early.

### The problem in one paragraph

A browser always runs code this server handed it seconds ago, so client and
server literally cannot disagree. An installed app can be six months old,
sitting on a phone you have no access to, and it will keep calling whatever
endpoints it was built against until its owner chooses to update. Every
mechanism below exists to make that survivable.

### What is already in place

**Versioned API with living aliases.** Endpoints mount at `/api/v1/…` and
also at the bare `/api/…` paths they have always had. When a shape changes,
`v1` KEEPS SERVING THE OLD SHAPE and the new shape goes to `/api/v2/`. Old
builds keep working instead of going blank. Removing an alias is a MAJOR
change, never a tidy-up.

**A client support floor.** `GET /api/v1/meta?client=1.2.3` reports the
build, the API version, and whether that client is still supported. A native
app calls this on launch and shows "please update" instead of rendering a
blank screen against a contract it no longer understands. Unauthenticated on
purpose — a client must be able to discover it is too old to log in.

**A stamped schema with a downgrade guard.** `app_meta.schema_version`
records what shape the database is. A build refuses to start against a
database NEWER than itself, because the classic disaster is rolling back to
an older image, which then writes rows silently missing the new columns and
does damage only visible weeks later. Refusing to boot is deliberate: a
warning would be read after the writes.

**Carrier callsigns as configuration.** `PT_HOME_CALLSIGN` and
`PT_CANDIDATE_CALLSIGNS` (see `app/carriers.py`). Not branding — strip them
and deadhead resolution stops working. Now a crew member at any airline sets
two environment variables instead of editing code.

**Icon styles by key, not by file.** `ICON_STYLES` in `main.py` maps to
filename stems generated by `make_icons.py`. Adding a style is a dict entry
plus a re-run, and the map marker and app icon are generated from one source
so they cannot drift.

### What is deliberately NOT done yet

Listed so nobody "discovers" them as oversights:

- **The API still returns formatted display strings** (`dep_line`, `ete`,
  `dep_shown`) and takes `time_format` as an argument, so the SERVER
  formats. A native client cannot use those. The fix is additive — emit an
  ISO-8601 UTC value and the airport's IANA zone alongside each pretty
  string, as `enriched_at_iso` already does — so it does not need a v2. See
  ROADMAP P1-1. **Do this before writing any native client.**
- **No token auth.** Cookies only. Fine in a browser and inside a WebView,
  wrong for a native client, and painful to retrofit through a whole app
  later. ROADMAP P1-3.
- **No push infrastructure.** The poller already knows the moments worth
  notifying (wheels-down, gate-in, delay published, diversion); nothing
  records them yet. ROADMAP P1-6.
- **Leg confirmation does not exist yet.** The logbook export depends on a
  pilot confirming which legs actually flew (NEXT UP / N3). Until that
  lands, nothing should export flight times anywhere.
- **Migrations are column-level and idempotent, not numbered.**
  `SCHEMA_VERSION` makes numbering possible; nothing needs it yet. When a
  migration first has to be ORDERED rather than merely repeatable, add the
  numbered runner then — not before.

### Rules for whoever ships the native client

1. Call `/api/v1/meta` on launch. Handle `supported: false` with an update
   prompt, never a blank screen.
2. Never parse a display string. If a value is needed as data, add a machine
   field to the API; do not regex the pretty one.
3. Send the client version on every request so the server can measure what
   is actually in the wild before deciding what it can drop.
4. Assume every version you ship lives forever on somebody's phone.

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

**Zone LABELS come from `view.zone_label()`. Always.** Never call
`tzname()` elsewhere, and never derive a label from a hard-coded sample
date — that is what made every label the summer one year-round. Pass the
date being displayed so daylight time is answered for the right day.

**Every time displays its own zone, as a superscript.** No surface
suppresses a label by comparing departure to arrival. The superscript is
what makes that affordable — at full size a zone beside every time wrapped
rows on a phone, which is what drove the old suppression rule. Lift it with
`transform`, never `vertical-align: super`, which grows the line box and
spaces the rows apart. Where a compact RANGE is shown on one line
(calendar agenda, import review, admin), one suffix covers it: `CT` when
both ends match, `CT/MT` when they do not. Those are the only two patterns
permitted.

**Wall-clock to UTC goes through `app/timezones.py`. Always.** Never call
`datetime.combine(d, t, tzinfo=ZoneInfo(...))` anywhere else. That form
silently invents an instant for a time that does not exist (spring forward)
and silently picks one of two for a time that happens twice (fall back).
Never infer an arrival DATE by comparing local clock times — the two clocks
are in different zones. `tests_timezones.py` enforces this by scanning the
package.


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
9. **Every figure on the route strip requires a live fix.** Progress,
   distance AND ETE come from a position, or they do not exist. No fix →
   no figures, nothing drawn, and never a number derived from the clock.
   They appear together or not at all: one live-looking figure beside two
   blanks is worse than three blanks. Progress returns None before
   departure too — a pinned 0% looks like a measurement and is not one.
10. **Aircraft identity is the ICAO hex, never geometry.** Heading-based
    rejection breaks on diversions, holds and opposite-flow departures.
11. **Booleans from SQLite are `0`/`1`, not `False`/`True`.** Normalise
    before `is False` comparisons.
12. **Never reuse a table name across schema versions** without checking
    `PRAGMA table_info`. `CREATE TABLE IF NOT EXISTS` silently no-ops.
13. **Deleting a user deletes their `roster` rows only.** Flights and
    tracks are shared.
16. **A missing asset must not take the page with it.** Anything that
    depends on a separately-loaded file guards for its absence and degrades
    to something honest. v6.1 put the scroll reveal inside the map's IIFE:
    Leaflet 404'd on deploy, every line below it threw, and the page lost
    its map AND its schedule at once. Nothing that hides content in CSS may
    rely on script to bring it back.
15. **No page loads a script or stylesheet from someone else's server.**
    Leaflet came from unpkg.com in a render-blocking tag, so a slow or
    unreachable third party produced a white screen while this box sat
    healthy. Everything render-blocking is vendored under `static/vendor/`.
    Map TILES and the radar layer are live services and stay remote — lose
    them and you get an empty map inside a working app, which is survivable.
14. **Retention is the only thing that deletes a flight.** Not "nobody has
    it on their schedule any more" — that describes a roster, not a
    real-world flight, and deleting on it destroyed real data every time a
    test schedule was imported. Un-rostered rows age out at
    `RETENTION_DAYS` with everything else. Nothing polls them meanwhile.
17. **A closure rule is worthless unless something still asks it.** Every
    route in `closure.decide` assumed the leg is judged forever; selection
    stopped judging at scheduled arrival + 3h, and three of the five routes
    silently became unreachable. Whenever you add a rule that matures on a
    clock, check that the thing which CALLS it is still running by then.
    `poller._closeout_sweep` is that guarantee and costs nothing, because
    everything closure reads is already stored on the row. (1.5.0)
18. **An import ADDS. Only a human removes.** `merge_schedule` and
    `remove_legs`, never `replace_schedule`, on any user-facing path. Scope
    reconciliation to the months the paste covers and to legs that have not
    departed — a bid line is a statement about one month and about the
    future, and reading it as a statement about the whole roster is what
    made pasting September erase August. A past leg can only be removed by
    a person deliberately deleting it. (1.5.0)
19. **A leg that is DOWN gives up the card.** Selection asks whether a leg
    has STARTED, and a finished leg has still started, so leg 1 held the
    card through its whole three-hour grace while the crew boarded leg 2.
    `_on_ground` is deliberately broad here — six signals, any one enough —
    because unlike everywhere else in this app, the cost of being wrong is
    a stale card that self-corrects, not a flight ended early. (1.5.0)
20. **A simulated leg never spends, never asks ADS-B, and never counts.**
    `flights.simulated` is checked at the top of `enrichment.refresh` and
    `backfill_gate_in` before the API key is read, in `poller` before
    `live_state`, and in the gate-in sweep's own SQL. Any logbook or export
    added later MUST filter it too. Beyond the money: a real flight
    somewhere may share an invented callsign, and one leak mixes invention
    with fact in a single row with no way to tell them apart afterwards.
    (1.6.0)
21. **The last admin cannot be removed.** `auth.set_admin` refuses, and an
    admin cannot demote themselves. An install with flight data and no
    administrator has no recovery path short of editing SQLite on the NAS
    by hand. (1.6.0)
22. **One aeroplane, from one source.** `make_icons.py` generates the PNGs
    AND `static/planes.js`; the tab bar reads
    `partials/plane_glyph.html`; the progress bar uses the same path. Every
    style is a SINGLE closed path, because the map strokes an outline round
    every shape it is given. Adding a second shape "just for the map" is
    what produced three different aeroplanes. (1.7.0)
23. **The diagnostics page must not be the broken thing.** Two 1.8.0 bugs
    lived only there: the probe bypassed the ADS-B throttle and reported
    every feed dead, and the active-flights list included closed legs and
    fired live lookups at them. Anything this page does upstream goes
    through the SAME throttle and the SAME guards as the poller, or it
    reports on an app that does not exist. When the probe and the real
    lookup history disagree, the history wins. (1.8.0)
24. **Nothing rides on a background of its own colour.** The progress-bar
    plane was `var(--accent)` on a `var(--accent)` fill and was invisible
    until it moved past it. Anything drawn ON a themed surface needs paint
    that reads against every state that surface has. (1.7.0)
25. **A flight is drawn by `.fstrip`, in one of its three sizes.** The
    tracker card, the tracker list and the calendar agenda each had their
    own markup for "flight number, city pair, two times" and had already
    diverged on which times carried a zone and what the colours meant.
    Same shape of failure as the eleven copies of the palette (v5.9) and
    the three producers of a zone label (1.2.0). The component lives in
    `static/app.css`; `--lg`/`--md`/`--sm` override CUSTOM PROPERTIES ONLY.
    A size that has to restate a layout rule is a fourth copy wearing a
    modifier's name — add a variable instead. Contextual overrides
    (`.fstrip-head .status`) belong to the surface and are fine. (1.9.0)
26. **Lateness is narrated on the ARRIVAL only.** Colour still marks both
    ends, and the departure keeps the time it moved from struck through
    beside the one that replaced it — nothing is concealed. But the WORDS
    "late" and "early" appear under the arrival alone: a leg that pushed
    twelve minutes late and lands on time is not a late flight, and the
    people reading this app are asking when he gets there, not when he
    left. (1.10.0)
27. **When one fact is drawn twice, fix both or neither.** 1.10.0 rebuilt
    the card's expanded panel and left the flight list's `renderLegDetail`
    — a second renderer for the same information — untouched, so the
    release read as undeployed to the person who happened to tap the other
    one. Before changing how anything is displayed, grep for every surface
    that displays it. This is invariant 25 stated as a working habit
    rather than as a component. (1.10.1)
28. **Green means EARLY. On time is plain.** Not a style preference: green
    on every normal flight is wallpaper, which is the same argument that
    killed the on-time pill, AND it would make "the airline reported on
    time" indistinguishable from "the airline has reported nothing" — two
    states this app is careful to keep apart everywhere else. Red is late
    or cancelled. Plain is on-schedule or unreported. The disc beside a
    time takes that time's colour, never a fixed one for its direction.
    (1.9.0)

29. **The page does not scroll; the sheet does.** Six things once agreed
    about `window.scrollY` — scrim, schedule opacity, pointer-events,
    heads-up controls, card height, map framing — and three releases were
    spent on what happened when they disagreed. The schedule's scrollbar
    is its own. Nothing new may be driven from the document's scroll
    offset. (1.12.0)
30. **A string in a template is not a running line of code.** "Show on
    map" sat outside every script block for five releases while a test
    asserted its presence and passed. Where POSITION decides whether code
    executes — inside a script block, inside the right closure, before the
    thing it calls — assert the position. (1.12.0)

31. **Check div balance after removing markup.** A stray `</div>` is not a
    syntax error, it is a different document: it renders, returns 200 and
    passes every suite while nesting content somewhere nobody intended.
    1.13.0 shipped one and it cost two visible bugs. The check is three
    lines and belongs after any edit that deletes elements. (1.14.1)

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
simulator.py    test mode. Produces POSITIONS only; the app judges them.
importer.py     what a paste would CHANGE. Describes, never applies.
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
| `observed` (long stop) | landed + stationary 30 min, **transmitting or not** |

Observed closes the leg **even with an API key** (owner's decision).
`actual_in` is the OOOI field most often missing; v5.0 waited on it and
hung. A late airline gate-in **upgrades** an observed/backstop close — and
as of 1.5.0 that upgrade can actually happen, see below.

Both halves of the SHORT `observed` route are required: a plane holding
off-gate stays stationary while transmitting; a coverage hole is silence
without a stop.

**The LONG STOP route (1.4.0) exists because requiring silence everywhere
created a trap with no exit.** An aircraft that lands, taxis in, parks and
keeps transmitting — ordinary, especially with the APU running — never
reaches `SIGNAL_GONE_MIN`. `observed` could not fire, and the backstop's
`quiet` test could not either. The only remaining exits were an airline
gate-in (the OOOI field most often missing) and `relaunch`, which on the
last leg of a day means the following morning. So the leg sat in taxi-in
indefinitely and, because it never closed, **the next leg never became
current** — the app appeared frozen on a finished flight.

Thirty minutes stationary is itself the evidence. Pairing five minutes with
silence is a fair way to tell "parked" from "holding for a gate"; at thirty
it is not, and closing early is much the smaller error than blocking every
leg behind it.

**The 1.4.0 fix was correct and could not work, because nothing was asking
(fixed 1.5.0).** Every rule in this table was written as though a leg is
judged forever. It is not. `poller.active_flights()` returns only the
current leg and imminent upcoming ones, and `get_current_info` releases a
leg 3 hours past its SCHEDULED arrival unless it is demonstrably still
airborne. After that instant nothing in the app ever looked at the leg
again. So three of the five routes expired together:

  * the **backstop** matures 3h past the REVISED arrival, which on any late
    flight is later than 3h past the scheduled one — so on exactly the
    population it exists for, the leg was abandoned before its own backstop
    came due;
  * **relaunch** needs a later sweep to notice the aircraft flying again;
  * the **long stop** needs a sweep at the 30-minute mark, which a leg
    blocking in near the end of its grace never got.

Reported as: blocked in at 07:00, still open at 11:30.

**Fix: `poller._closeout_sweep`.** After each normal sweep, unclosed
rostered legs from the last 7 days are re-judged. It costs NOTHING and is
deliberately kept that way — every value `closure.decide` reads is already
on the row, so this is pure re-evaluation with no ADS-B and no AeroAPI
call. `stopped_since` and `last_signal_at` are timestamps, so stopped-for
and signal-gap keep growing correctly without anyone fetching anything. A
leg still inside its live window is skipped, so the two sweeps can never
both judge one leg in a pass.

**The upgrade path was a door with a wall behind it (fixed 1.5.0).**
`maybe_close` has always been able to upgrade a provisional close to the
airline's own gate-in. It could never fire: a closed leg was never polled
again, and `should_query` refuses to spend on one, so the single value that
could trigger the upgrade was the one value nothing would ever fetch.

**Fix: `poller._gate_in_sweep` + `enrichment.should_backfill_gate_in`.** A
leg that closed on anything other than `airline` and is still missing
`in_actual_api` gets three late attempts — +90 min, +6 h, +18 h from the
previous attempt, reaching roughly 24 hours past block-in. The owner's
report set those numbers: usually the airline is quick (already covered by
the leg's own live tickets), but a 07:00 block-in had nothing by 11:30.
Attempts are recorded BEFORE the call goes out, the `carrier.py` lesson, so
a timeout still counts. A silent airline costs exactly three queries no
matter how many thousand sweeps run — under two cents, enforced by test.

This matters more for the logbook (N2/N3) than for the tracker:
`in_actual_api` is what an export is allowed to use, because an observed
time must never masquerade as a reported one in a legal record.

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
  * ...but a leg that is DOWN hands the card on as soon as the next leg's
    window opens (`_on_ground`, 1.5.0). The rule above, alone, meant a
    landed leg held the card for the full three-hour grace while the crew
    were already boarding the next one — because selection asked which leg
    had STARTED, and a finished leg has still started. Fixing closure in
    the same release did NOT fix this; selection never asked whether a leg
    had closed.

`_on_ground` is deliberately BROAD, and that is the opposite of how the
rest of this app reasons. Everywhere else — `has_departed`, `_has_started`,
closure's guards — the demand is for strong evidence, because the cost of
being wrong is ending a flight that is still going. Here the cost runs the
other way: being wrong shows a family member a finished leg while the pilot
is boarding the next one, and the recovery is automatic, since the moment
the aeroplane is airborne again `_still_flying` takes over. So any ONE of
six signals is enough: `closed`, `landed_seen`, `on_actual_api`,
`in_actual_api`, `in_observed`, or `airborne_seen AND last_on_ground`.

That last one carries the outstations. `landed_seen` needs a SUSTAINED
touchdown to be observed, which a field with no ground coverage never
provides; but an aircraft we watched get airborne and are now seeing on the
ground has landed, whatever the confirmation timer thinks. The
`airborne_seen` half is what keeps pushback from reading as arrival.

Handover is ONE LEG AT A TIME — the earliest later candidate whose window
has opened, not the last leg of the day — so a four-leg duty steps forward
properly.

`MAX_AIRBORNE_HOLD` (12h) is the ceiling, so a stuck `airborne_seen` flag
cannot own the card indefinitely. A candidate that loses is appended to
`past`, not dropped — it is behind the leg now flying.

## PAGES

| URL | What it is | Who |
|---|---|---|
| `/` | the tracker card and map | pilot + viewers |
| `/calendar` | one month at a time | pilot + viewers |
| `/flights` | ONE PILOT'S SCHEDULE — paste, leg list, month filter | pilot |
| `/admin` | THE INSTALL — people, test mode, diagnostics, decision log | admins |
| `/settings` | how the app behaves for you | pilot + viewers |

**The `/flights` and `/admin` split is 1.7.0 and it was a real confusion,
not a tidy-up.** The tab bar had labelled the schedule page "Flights" since
v7.5 while its URL said `/admin`; 1.6.0 then piled the install's
administration onto that same page, so somebody's trip list and the control
that deletes every account were in one scroll under a name that matched
neither. Now the word means what it says.

Redirects are kept for every moved URL — `/admin/diagnostics` →
`/admin#diagnostics`, `/admin/debug` → `/admin#log`,
`/settings/users/delete/{id}` → `/admin#people`. A phone with a page still
open from before the update posts to the old path, and a 404 on a Delete
button is the worst possible way to learn a route moved.

## ONE AEROPLANE

**There were three.** The app icon cut one silhouette, the map marker cut a
second (the same shape with its engines deleted), and the tab bar and
progress bar had a third hardcoded inline that the icon system never
touched. Changing the icon style in Settings moved two of the four.

Now: `make_icons.py` is the single source. It generates the PNGs *and*
`static/planes.js`, which the map reads; `templates/partials/plane_glyph.html`
carries the same path for the tab bar; the progress bar uses it inline. A
path edit in `make_icons.py` changes all four or none.

**Every style is a SINGLE CLOSED PATH, and that is load-bearing.** "Modern"
used to be three overlapping shapes plus two nacelle rectangles. Filled
flat on a tile that is fine. On the map it is not: the marker strokes a
dark outline round *every* shape, so outlines ran through the middle of the
aeroplane and at 40px over a busy tile the whole thing read as a smudge.
One closed path strokes once, on its own edge. `paint-order="stroke"` puts
that outline *under* the fill so it reads as a halo rather than eating into
the silhouette.

**The progress-bar plane was invisible.** It was `color: var(--accent)`
riding on a `.route-fill` that is also `var(--accent)` — the same colour as
the bar it sits on, so it only appeared once it had moved past the fill. It
now has its own paint: card-coloured body, accent outline, which reads
against the flown half on its left and the empty track on its right, in
either theme.

**The tile has artwork now.** Night-sky gradient, the earth's limb across
the bottom, a great-circle arc, three stars, and the plane banked along the
arc at its apex rather than sitting nose-up in the middle of a square.

**The arc is split, and that is the point:** solid behind the plane, dashed
ahead of it. Same flown/remaining reading as the route strip on the tracker
card, so the icon says what the app *does* rather than just being a plane
in a box.

Everything in `_backdrop()` is a fraction of the icon size, so the 16px
favicon is the same picture as the 512px tile and not the same picture with
a giant arc across it. Below 64px the fine detail is dropped rather than
shrunk — a 3-unit dash pattern and a 1-unit star are mush at favicon size,
and mush reads as a smeared icon, not as detail. Maskable icons skip the
artwork entirely: Android crops them to whatever shape the launcher wants
and only the inner 80% is guaranteed, so an arc drawn to the edges would be
sliced at an arbitrary radius.

## TEST MODE

**Admin page → Admin · Test mode.** Rehearse a flight without waiting for a
real one and without spending an AeroAPI credit.

Every bug this app has shipped was found the same way: the owner flew a
trip, something looked wrong, and the evidence was gone by the time anyone
could look at it. Reproducing a closure bug used to cost a duty period.

**What it does, and what it deliberately does not.** The simulator produces
ONE thing: position reports, in the exact shape `livesource.live_state`
returns. Those then go through the same `flightmatch.observe`,
`flightmatch.evaluate`, `tags` and `closure.maybe_close` a real flight
does. If closure is wrong, test mode is wrong in the same way and by the
same amount — which is the only property that makes it worth having. A
simulator that wrote `closed = 1` directly would prove nothing.

**Three isolation rules**, each guarding a different way this could do harm:

| Rule | Enforced at |
|---|---|
| Never spend | `flights.simulated = 1` checked at the top of `enrichment.refresh` AND `backfill_gate_in`, before the key is read |
| Never ask ADS-B | `poller` routes a simulated leg to `simulator.state_for`, so the shared rate limiter is untouched |
| Never count | excluded from the gate-in sweep; **must** be excluded from the logbook and any export (N2/N3) |

Beyond the money, the first rule matters because a real flight somewhere
may share an invented callsign, and letting its data into a simulated row
would mix invention with fact in one place. Simulated legs use flight
numbers from 9900 up, which no US regional operates, so an invented leg can
never collide with a real one on the shared flight id.

### Scenarios

Each is named for the BUG IT REPRODUCES, not the flight it describes.

| Scenario | Proves |
|---|---|
| Normal leg | the happy path; closes on the short observed route |
| Taxi-in trap | 1.4.0 — parks, keeps transmitting, no silence anywhere. Age 30m |
| Coverage lost in cruise | never seen to land; only the backstop can end it. Age 3h |
| Blocked in, no airline gate-in | 1.5.0 — the abandonment cliff. Age 3h |
| Two-leg turn | 1.5.0 — the card hands over the moment leg 1 is down and leg 2's window opens |
| Scheduled, never departs | the `has_departed` guard; nothing may close this, at any age |

### Why there is no speed control

The obvious design is a clock multiplier. It was rejected: the poller, the
card, `get_current_info` and every stored timestamp run on the real clock,
so a leg running at 60× is judged at one time and displayed at another, and
every discrepancy that produced would be a property of the simulator rather
than of the app.

Two honest mechanisms instead. Scenarios use SHORT legs (10–14 minutes of
block time), so a full gate-to-gate happens while you watch. And for rules
that mature on a long clock, **Age this leg** shifts the row's recorded
timestamps backwards. Nothing is faked and no threshold is lowered: after
ageing 30 minutes the leg genuinely has been stopped for 30 minutes, and
the real production rule fires on the real number with no knowledge that
test mode exists. Ageing never touches `date`, `dep_time_local` or
`arr_time_local` — those define the leg's window, and moving them would
change which rules are in play rather than just making the leg older.

**Stop & delete** removes simulated legs outright, with their roster rows
and tracks. Not retired — retention protects a record of flights that
happened, and a rehearsal did not happen.

## ADMINISTERING THE INSTALL

Everything that operates the install is on the **Admin page** (1.6.0).
Settings is where a pilot changes how the app behaves for *them*; /admin is
where whoever runs the box runs it. On a shared install those are two
different people, and only one of them sees the admin panels.

See PAGES above for the full split and the redirects that keep old links
working.

### Adding a second admin

**Before 1.6.0 there was no way to do this at all.** `create_user` sets
`is_admin` on whoever registers first and nothing else ever touched the
flag — so on a self-hosted box, losing the first account lost
administration of the install permanently, with the data still sitting
there.

1. Send them this server's `/register` page. They create their own account.
2. `/admin` → **People** → **Make admin…** → **re-enter your own password**.

The password gate is 1.7.0. A single tap was too little friction for an
irreversible grant — the person you promote can then see and delete every
account on the install, including the one that promoted them. Re-entering
the password also means an unlocked phone left on a crew room table is not
enough on its own. Demotion is gated the same way.

**The last admin cannot be demoted or deleted**, including by themselves.
Removing the final admin leaves a database with real flight data that
nobody can administer, and there is no recovery path from the app — it
would mean opening SQLite by hand on the NAS.

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

**One exception, added 1.5.0: the late gate-in chase.** The rule above is
right for the LIVE allowance — once a leg is closed there is nothing left
to watch. But it also meant `in_actual_api` could be permanently missing on
any leg whose airline reported late, and that is the one figure a logbook
export is allowed to use. So a closed leg that is still missing gate-in
gets up to three attempts on a SEPARATE allowance (`gatein_tries`, not
`api_queries_used`) at +90 min / +6 h / +18 h. Two different questions, so
two different counters and two different functions —
`should_backfill_gate_in`, never `should_query`.

Worst case is 3 queries × $0.005 on only those legs that closed without an
airline gate-in: about **$0.46/month** if every one of 46 legs needed all
three, and realistically pennies, since most legs get gate-in from their
own live tickets and never enter the chase at all.

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

## ADS-B FEEDS

**airplanes.live withdrew its free API in 2026.** Their reasoning, from the
notice sent to every API user: 2 billion requests a week, the month's egress
allowance gone in four days, hosting up nearly 300% in 18 months, and AI
agents and scrapers named directly as the cause. It is now $25/mo
sponsorship — **or free from a feeder's own IP.**

Defaults as of 1.8.0, tried in order until one answers:

| Feed | Access | Terms |
|---|---|---|
| `api.adsb.lol/v2` | open to everyone | ODbL 1.0 |
| `opendata.adsb.fi/api/v2` | open, 1 req/sec | personal, non-commercial; attribution required |
| `api.airplanes.live/v2` | **off** — feeders and sponsors | enable it if you feed or sponsor |

All three are ADSBexchange-v2 compatible, which is why swapping between
them is a URL change and not a rewrite. Attribution for adsb.fi and adsb.lol
is on the diagnostics panel, where their terms require it to be.

**If the feed list on your install still names a dead feed**, use *Reset
feeds to current defaults* on the admin page. `load_endpoints` prefers a
list saved in the database over the built-in one, so an install that ever
saved a list stays pinned to it while a fresh install of the same version
works — a difference no amount of reading the code explains.

**The best long-term answer is to feed.** A ~$30 RTL-SDR dongle and an
antenna on the NAS earns free API access at airplanes.live and adsb.fi's
feeder endpoints, adds coverage where the pilot actually flies, and removes
this whole category of problem. It is the only option here that gets better
rather than worse over time.

## COST CONTROL

`/flights/{ident}` costs $0.005 per result set; `/schedules` costs $0.02,
four times as much, which is why deadhead carrier resolution is capped,
counted at four units, and stored permanently once it succeeds.

At 18 tickets per leg, a heavy 50-leg month has a hard ceiling of $4.50
(plus at most $0.75 of late gate-in chasing if every single leg needed it,
which would mean the airline never reported once all month), and real spend
lands well under because most legs stop early the moment gate-in arrives. The per-pilot monthly limit is a hard stop on top of that
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

- **`viewer.html` loses JavaScript to colliding edits.** It has happened
  twice: in v4.5 nothing toggled `#expand-details` and `togglePast()` was
  called by an inline `onclick` but never defined; both were restored, and
  `#expand-wrap` no longer needs toggling at all as of v5.6. The failure
  mode is silent — the page still renders, a control just does nothing.
  `test_template_contract` in `tests_ui_fixes.py` now guards this, so run
  that suite after any template edit rather than grepping by hand.

  A related trace of the same failure sits above `applyEnrichment()`, where
  the docstring for `selectLeg()` has been absorbed into the following
  comment block. Verify after every multi-part edit.
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
python tests_flight_row.py          #  68
python tests_poller_end_to_end.py   #  47
python tests_past_leg_detail.py     #  19
python tests_budget_limit.py        #  17
python tests_carrier_cap.py         #  13
python tests_ui_fixes.py            # 527
python tests_app_shell.py           # 167
python tests_timezones.py           #  68
python tests_closeout_sweep.py      #  42
python tests_import_merge.py        #  39
python tests_test_mode.py           # 133
```                                  # 1140

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
- **Flight rows are SHARED BY ID.** A fresh user is not a fresh fixture. Two
  test cases both using flight 6001 on the same date write to the SAME row,
  so case two starts with case one's `landed_seen` already set and passes
  for entirely the wrong reason. Give each case its own flight NUMBER.
- **A rule and its caller are two separate pieces of correctness.** Driving
  `closure.maybe_close` directly proves the rule; it proves nothing about
  whether anything still calls it at the moment it matures. That gap hid
  the 1.5.0 abandonment cliff behind 714 passing tests for two releases.
  `tests_closeout_sweep.py` runs `poll_once` PAST the three-hour grace for
  exactly this reason.

### THE PANEL REBUILD (owner's design, in flight)

The hero card floating over the map goes away. The flight LIST becomes the
default surface, resting about a third of the way up; tapping a flight
slides a DETAIL panel up over it, with an X to go back. Modelled on the
reference app, screenshots in the owner's brief.

| Pass | What | State |
|---|---|---|
| 4 | the strip itself: overlap, gate-only | **DONE 1.12.1** |
| 1 | the two panels, the slide between them, hero card deleted | **DONE 1.13.0** |
| 2 | selection drives the map; whole trip dashed at rest, active leg primary | **DONE 1.14.0** |
| 3 | current trip only; leg drops 30 min after closeout; 10-hour handover | next |

DECIDED, so it does not get re-litigated:

- **Progress bar lives on the hairline between the two airport blocks** —
  the rule that already carries block time and distance. With a live fix
  it fills and carries the aeroplane; without one it stays a plain rule.
  NOT floated over the map: the map already shows progress (that is what
  the marker is), and a floating bar would need its own position logic
  against two sliding panels, which is the coupling 1.12.0 deleted.
  A second, quieter one: a tinted hairline along the bottom edge of the
  live flight's row in the list, so the moving leg is visible without
  opening anything.
- **Auto-open the detail when a leg is airborne**, on load only, and
  NEVER re-open after the user has closed it. It must be the same panel
  the tap opens — not a variant — or there are two things to maintain.
  This is also what keeps the progress bar visible by default, which is
  the whole reason it matters.
- **The last leg of a trip does not vanish.** The 30-minute removal
  applies only while later legs remain; otherwise 30 minutes after the
  final arrival the list would be empty for the rest of the 10-hour wait.
- **ADS-B stays at the bottom of the detail panel.**


## VERSION HISTORY

### 1.14.1 — first real device pass on the rebuild

Nine issues from the owner's first proper look at 1.14.0. One of them was
the cause of two others.

**BUG: a stray `</div>` broke the panel.** Removing the times row from the
panel header in 1.13.0 took out ONE opening div and left BOTH closers, so
`#collapsed-card-header` closed early and most of the panel's content ended
up outside it. That is why the detail panel opened only a couple of inches
and why two orphaned "CT" zone labels floated over the map. The template
still rendered, still returned 200, and every suite passed — an unbalanced
div is not a syntax error, it is a different document.

A div-balance check existed and was run against the SHEET edit in 1.12.0.
It was not run against this one. It is cheap and it belongs after any edit
that removes markup.

**The sheet is fixed in place.** Two snap points plus a drag was three ways
to end up somewhere you did not mean. No drag, no toggle, no second
height; the list scrolls inside it. The grab bar became a real "Flights"
header, because a handle advertising a gesture that does nothing is worse
than no handle.

**The row dropdown and "Show on map" are both gone.** The dropdown opened
a second, smaller detail view inside the row — its own renderer, its own
label/value list, its own map link — while a row tap now opens the full
panel. Two detail views for one flight is the thing this rebuild set out
to stop. `renderLegDetail`, `.row-detail`, `.rd-skeleton` and `.row-caret`
go with it.

**The whole-trip outline is removed, one release after adding it.**
Recorded rather than quietly reverted: it was reasonable when nothing else
drew a leg, and wrong the moment tapping a row redrew the map — every
other leg stayed underneath the one being looked at, so "where is he"
competed with four faint lines that answered nothing. Owner's call.

**Strip layout.** The two ends sat at opposite edges of the row to line up
with a progress track that ran between them; that track left with the hero
card, so the split was leaving a lake of white space and putting the
arrival time as far from the departure as the screen allowed. They read as
one phrase now. Status pills moved beside the flight number where the eye
already is, and the live dot went: the blue edge down the row already says
which flight is happening.

Tests: 1,151 → 1,140, all deletions.

STILL OPEN: the expanded view has not been seen yet — it was unreachable
until this release — so pass 3 waits on a look at it.

### 1.14.0 — tapping a flight means that flight

Pass 2. Small, and it fixes something 1.14.0's predecessor got wrong.

**BUG SHIPPED IN 1.13.0: the panel opened without selecting.** Tapping any
row opened the detail panel showing whichever leg was ALREADY selected —
usually the live one. The worst kind of wrong: nothing blank, nothing
thrown, no error in the console, the numbers simply somebody else's. It
happened because the panel and the selection were wired in different
releases and nothing asserted they met. There is a test now that reads the
row's own id out of the handler and checks it is used before the panel
opens.

`selectLeg` is exposed as `window._ptSelectLeg` because it lives in the
polling IIFE, which is parsed after the sheet's handler. ONE selection
path, shared with "Show on map" — two would be two ways to select the
wrong leg. And because `selectLeg` already redraws the map, this single
call is also what makes "tapping a flight shows it on the map" true,
without a second mechanism to keep in step.

**The whole trip is outlined on the map**, faintly, dashed, behind the leg
being tracked. Drawn ONCE and never touched again: a trip does not change
shape as an aeroplane moves along it, and anything inside renderMap's
clear-and-redraw cycle is torn down and rebuilt on every poll.

Faint and straight on purpose. These are pairs of airports joined by a
line — not routes flown, not routes planned — and drawing them any more
confidently would claim something the app does not know. The heavy solid
line stays reserved for a track that actually happened, which is invariant
9's spirit applied to the map rather than to a number. Non-interactive, so
the outline cannot steal a tap from a real marker.

`trip_routes` is derived from the flight groups already built for the
list, not from a second walk of the roster: whatever the tracker is
listing is exactly what the map should outline, and computing them
separately is how the two come to disagree.

Tests: 1,139 → 1,151.

### 1.13.0 — the hero card is gone

Pass 1 of the panel rebuild, and the largest structural change since the
app was written. The card that floated over the map is deleted. The flight
list is the default surface; tapping a flight slides a DETAIL PANEL up
over it, dismissed with an X or Escape.

**What actually went.** Not just the card — the engine under it. A spacer
stretched by measurement until the card's bottom edge sat just above the
tab pills, recomputed on resize, on rotation, on every ResizeObserver
tick, and on every open and close, plus `capPanel()`, `minTop()`,
`tabTop()`, `slidePanel()`, `setExpanded()`, `foldEnds()`, `measureEnds()`
and the `endsH` correction. Also `.hero-space`, `.card-more`, the caret,
the "Flight Details" row, and the 1.10.1 fold that hid the strip's times
when the panel opened.

**Every bug that machinery was written to fix is now unreachable rather
than fixed.** The creeping bottom edge (1.10.1, 1.10.2) needed a card
whose height changed while its position was computed from that height. The
panel is `bottom: 0` and moves with a `transform`, so its contents do not
reflow while it slides and there is no position to compute. It cannot
outgrow the screen because `max-height` says so. It cannot disturb the map
because it shares no coordinate system with it.

Transform rather than height is deliberate and worth keeping: every
height-driven animation in this file has cost a bug, the same one twice.

**The panel header carries no times.** The airport blocks below give both
at three times the size with the original struck through beside them,
which is exactly what the folding row was working around.

**Auto-open when a leg is airborne**, on load, ONCE. Never re-opened after
the reader closes it — the poller runs every few seconds and a panel that
reappears each time is a fight, not a feature. It is the same panel a tap
opens, not a variant, driven off the same `is_selected_live` flag as the
live ADS-B box so "the app thinks he is flying" cannot mean two things on
one page. This is also what keeps the progress bar on screen by default.

**The sheet drag has a threshold.** Twelve pixels before anything moves.
It felt grabby because a drag began on the first pixel, so every tap on
the handle nudged the height and snapped somewhere on release; a
sub-threshold move is now left to the click handler, which toggles
cleanly. Peek raised to 38vh — roughly where the hero card used to start.

**Test bookkeeping, and a mistake worth recording.** Three hero-layout
tests were replaced by one panel test, and two machinery tests
(`test_strip_times_fold_when_the_panel_opens`,
`test_map_refits_once_and_only_when_still`) by
`test_fold_and_refit_machinery_is_gone`, which asserts the deleted
functions stay deleted — the cheapest way to reintroduce those bugs is to
reintroduce the machinery.

A line-range edit while removing the hero tests also deleted
`test_viewer_theme_is_consistent_across_pages`, which had nothing to do
with any of it. Caught by diffing the test names against the previous
release and restored. Editing this file by line range rather than by
matched text is how that happened; do not.

Tests: 1,148 → 1,139. The drop is deletions, not lost coverage.

STILL TO DO (pass 2): tapping a flight does not yet redraw the map for
that leg, and the map does not yet show the whole trip at rest.

### 1.12.1 — the strip stops printing on itself

First pass of the panel rebuild, taken out of order because it is
self-contained and it is the thing most looked at.

**BUG: the zone printed under the next disc.** The ends row was a flex row
whose items carried `min-width: 0` — which lets a flex item shrink BELOW
its own content. The times are `nowrap`, so rather than shrinking they
spilled out of their box, and the departure's zone superscript landed
under the arrival's disc.

Not a marginal case. 12-hour time adds " PM" to both ends and about a
third more width, so the format most likely to be in use was the one
guaranteed to collide — and 12-hour was never checked when the strip was
designed in 1.9.0, despite the app supporting it and a test suite existing
for it. Each end is now `flex: 0 0 auto` and the row wraps, so an arrival
that genuinely will not fit drops to a second line, which is legible,
rather than printing on top of something, which is not.

Reintroducing `min-width: 0` to silence an overflow here would restore the
bug exactly. An overflow in this row means the content does not fit, and
the answer is to let it wrap, not to let it lie about its width.

**Gate only** (owner's call). The terminal line under each gate and the
baggage-belt badge are gone: a gate number already tells anyone using it
which terminal they want, so the line spent a row of every flight
restating its neighbour, and a belt is useful rarely and on screen always.
DISPLAY decision, not a data one — both are still fetched, still stored,
still in the payload, asserted by test. Bringing them back must never mean
re-querying the airline for flights already paid for.

Tests: 1,137 → 1,148.

### 1.12.0 — the flight sheet

Owner's design. The schedule moves into a bottom sheet that rests PEEKING
over the map, with its own scrollbar, opened by drag or tap. This deletes
a system rather than adding one.

**Why this was worth doing.** The page's scroll offset drove the scrim's
opacity, the schedule's opacity, the schedule's pointer-events, the
heads-up controls, the card's height and the map's framing — six things
agreeing about one number. Three consecutive releases went on what
happened when they did not: the creeping bottom edge (1.10.1, 1.10.2),
the map re-fitting twice per collapse (1.10.1), the map swallowing scrolls
meant for the schedule (1.10.2). And 3b — start the list on the current
flight — was declared not-worth-the-risk in 1.11.0 precisely because it
meant re-deriving all six. In the sheet it is one assignment to
`scrollTop`, because the sheet owns its own scrollbar and nothing else is
measured against it.

**It rests peeking, not shut.** A button that hides the schedule would
repeat a mistake this README already records: "Flight details" once hid
the entire list behind a small grey line of text, and the schedule is the
reason the page exists. Two flights are visible at rest; the rest is a
drag away.

**One substitution did most of the work.** The card was ALREADY
bottom-anchored and already grew upward into the map — it simply measured
against the tab pills. `tabTop()` now returns the sheet's top edge. Nothing
in `layout()` or `capPanel()` changed at all.

**Deleted:** `.scroll-scrim`, `.hero-space`'s reason for existing, the
scroll listener and its rAF paint, `.map-shield` (1.10.2). The shield went
because the document no longer scrolls, so there is no page scroll for the
map to steal — the root cause rather than the cover for it. `_ptPaintReveal`
and `_ptRevealOff` survive as no-ops only because other blocks call them.

**BUG, PRE-EXISTING SINCE AT LEAST 1.8.0: "Show on map" never worked.**
Nine lines — the entire click handler — sat after the closing html tag,
outside every script block on the page. Browsers parse content there as
TEXT, so it failed silently rather than erroring: there was nothing there
to error. Every row's dropdown has offered a dead link for five releases.

`tests_ui_fixes` asserted the feature existed and passed throughout,
because it grepped the template for the handler's own text. A string being
present in a file is not the same as it running. The replacement asserts
the POSITION — nothing after the closing html tag, no code outside a
script block, and the handler inside the IIFE where `selectLeg` is
actually declared, since `selectLeg` is not on `window`.

Found by chasing an unrelated `window.scrollTo` that the sheet made
obsolete.

Tests: 1,125 → 1,137. `test_scroll_reveal` became `test_flight_sheet`,
carrying forward the two lessons it existed to guard: the schedule must
not be hidden by CSS with the script relied on to restore it (v6.1), and
the sheet must not live inside the map's IIFE (v6.2).

### 1.11.0 — the tracker list is the trip you are on

Step 3, partly. The scope and the strips are done; the SCROLL POSITIONING
is not, and the reason is recorded below because it is a design problem
rather than unfinished typing.

**THE TRACKER NOW HOLDS THIS TRIP AND THE NEXT.** It rendered the entire
365-day roster and hid most of it behind Show-past-flights — a list that
grows without bound, pretending to be a list that does not. `trip_slices`
cuts the roster at `trip_start` markers; `tracker_window` keeps the
anchor's trip and its successor. The anchor is the live leg, or failing
that the next leg flown, which is the same fallback the card uses — so
the card and the list are always discussing the same trip.

Flown legs of the CURRENT trip stay, dimmed. "He has done three of
today's four" is the question this page exists to answer. Flown legs of
older trips leave entirely; they are the calendar's job.

Degrades toward the OLD behaviour, never toward a blank page: a roster
with no `trip_start` markers at all — pasted without the blank lines the
parser keys on — comes back as one trip containing everything, and an
anchor that cannot be placed returns None, meaning "no opinion, show
everything".

**Show past flights is gone**, and so is `togglePast` and the
anchor-and-scroll-back trick that stopped the page leaping to the oldest
flown leg when the section unfolded. None of it is needed once there is
nothing to unfold. `.is-past` no longer sets `display:none`; it dims.

**List rows are `.fstrip--md`** — the same component as the card. They
were bespoke markup with their own arrow icons, their own zone placement
and NO DELAY STATE AT ALL, so a flight could read plain in the list and
red on the card in the same breath. `view.strip_lines()` is the shared
middle: `build()` and the list now reach the same two `*_line` dicts
through the same `_variance` and `_time_line`. `time_index()` fetches the
six timestamps for every row in one narrow SELECT, following `tag_index`.

**NOT DONE: the list does not position itself on the live leg.** The
agreed behaviour was "scroll down from the map and land on the current
flight; scroll up for flown ones". That requires the flown legs to sit
ABOVE the card in the document, and they sit below it — the card is a
hero whose top edge is driven by a measured spacer, and moving content
above it means the spacer maths, the reveal fade and the map framing all
have to be re-derived. It is a real piece of work, not a line of CSS, and
doing it badly would put the card's welded bottom edge back in play after
three releases of fixing exactly that. It is 3b.

The pressure behind it is also much lower now: the flown section is at
most a handful of dimmed rows from the current trip, not hundreds.

Tests: 1,105 → 1,125. Four older assertions pinned the list's old markup
literals — the RULE they protected (every time carries its own zone
element, never suppressed by comparing the two) still holds and is now
matched on shape. A test that pins one surface's markup makes replacing
that surface look like a regression, which is what happened here.

### 1.10.2 — the creep, the jump, and the stolen scroll

Three motion faults on the tracker. No change to data, polling or layout
structure.

**BUG: the card's bottom edge still crept while opening.** 1.10.1 folded
the strip's times away and taught the spacer maths about it, and the
arithmetic was right — but the ANIMATION was not. A row's rendered height
is `min(natural, max-height)`, so transitioning max-height from its 4rem
resting cap down to zero stands still for the first third of the curve
and then collapses 22px over the remainder. The panel and the spacer,
meanwhile, move smoothly across the whole 260ms. The three stopped
cancelling out, so the edge drifted and then snapped back at the end.
`foldEnds()` now pins max-height to the MEASURED height before starting,
which makes the fold linear in height and the three cancel properly. The
lesson is not about CSS: the number was correct and the motion was wrong,
and only one of those is visible in a test that checks arithmetic.

**BUG: the map stole scrolls meant for the schedule.** The map is a fixed
full-screen layer behind the page, so it is still there below the card —
in the margins either side of it, in the gap above the tab pills, behind
anything the flight list does not physically cover. Leaflet takes a touch
through a transparent gap quite happily, so scrolling toward next week's
flights sometimes panned the map sideways instead, depending on where a
thumb landed. `.map-shield` is an invisible sheet from `--card-top` to the
bottom of the window at z-index 2 — above the map, below the page — with
`touch-action: pan-y`. The strip of map ABOVE the card stays live, because
that is the part you can see and panning it is the point.

**The map's re-fit glides instead of snapping.** `fitBounds` is instant by
default. When the card changes height the route has not moved, only the
window onto it, so an instant re-frame reads as the map losing its place.
`_ptRefit` now passes `animate: true, duration: 0.35`; the first paint and
the poll-driven fits stay instant, because there is no previous view to
ease away from. This is a mitigation, not a proof: 1.10.1 already stopped
the DOUBLE fit, and whether a correctly-timed single fit was the whole of
the remaining "jumping around" is not yet confirmed on a real device.

Tests: 1,089 → 1,105. One assertion added in this session was written
backwards — it asserted the instant call sites did NOT exist, when their
existence is the point — and was corrected before the release.

### 1.10.1 — finishing step 2: three bugs behind one symptom

Reported as "deployed 1.10.0, nothing changed, Closed out is still
there". The deploy was fine. Three separate faults were hiding behind
that one sentence, and the first is the reason the release looked inert.

**THE FLIGHT LIST HAS ITS OWN RENDERER, and 1.10.0 missed it.** There are
two expanded views in this app: the card's panel, and the dropdown inside
each flight-list row, which `renderLegDetail` builds as a second
label/value list in JavaScript. Every 1.10.0 decision was applied to the
card only. So the surface the owner happened to tap still said "Closed
out", still stacked its rows the old way, and the whole release read as
undeployed. Exactly the failure invariant 25 exists to prevent, committed
while writing the component that prevents it. "Closed out" is dropped
here and the arrival source now reads in English; the list's full move to
`.fstrip` is 1.11.0.

**BUG: the live box CONTAINED the flight detail.** `#flight-detail` was a
child of `#live-section`. Invisible in the source — the indentation
showed them as siblings, they read as siblings, and nothing in the panel
suggested otherwise — but `applyLegPayload` hides `#live-section`
whenever the selected leg is not the live one. So tapping any past or
future flight and opening the card produced a COMPLETELY EMPTY panel: no
times, no gates, no airport blocks. The times were never missing; they
were inside a hidden parent. Nothing pointed at the cause, because the
code doing the hiding names only the live box. Found while moving the
ADS-B block, not by looking for it.

**The ADS-B box moved BELOW the flight detail.** The panel opens directly
under the progress bar, so whatever comes first is what the reader lands
on. Altitude and groundspeed are the pilot's numbers; when he gets there
is everyone else's. This is also what un-nested the two blocks.

**The strip's times fold away when the panel opens.** The panel states the
same two times better — bigger, with the original struck through beside
them, with the gate attached — so leaving the summary up printed every
time twice, in two sizes, three inches apart. Animated on `max-height`
rather than `height`, because the zone superscript is lifted above its
own baseline by a transform and a hard height clips it.

The spacer maths had to learn about it. That row folding changes the
card's height mid-slide, and the whole `cardBase`/`target` dance exists to
weld the card's BOTTOM edge in place by predicting its height before the
animation runs. Left out, the bottom edge crept ~22px across the 260ms.
`endsHeight` is measured only while the card is SHUT and remembered,
because measuring on the way open reads a row that has already started
folding.

**BUG: hiding the details re-fitted the map TWICE.** `.expanded` comes off
before the 300ms slide begins — it has to, because `layout()` branches on
it — so the old guard let a fit run immediately, against a card height
300ms in the future. The settle pass at 320ms then measured the real
height and fitted again. Two `fitBounds` calls with different padding, a
third of a second apart: the map lurched out and back on every collapse.
`window._ptSliding` now suppresses the fit until the card has stopped
moving, and `lastTop` is deliberately not updated on a skipped fit so the
settle pass still performs the one real one.

**BUG: the card header jumped a frame into every slide.** A rule let the
city pair re-wrap to two lines the instant `.expanded` flipped, while
everything below it slid smoothly over 260ms. Dropped — since 1.10.0 the
panel spells out both airports in full, by name, so the summary above can
truncate consistently in both states, which is also what makes it
animatable.

Tests: 1,067 → 1,089. One older assertion was rewritten to match intent
rather than an exact expression: it pinned `cardBase + target`, which the
folding row legitimately changed, and a test that pins arithmetic makes
every future correction look like a regression.

### 1.10.0 — the expanded view, one box per airport

Step 2 of five. Presentation only: no change to polling, closure,
matching, budget or the schema.

**The old shape asked the reader to reassemble a flight.** One airport's
story was spread across four rows that were not even adjacent —
"Arrival" near the top, "XNA gate" two down, "Baggage" below that. But
nobody asks "what is the arrival time"; they ask "what do I need to know
about XNA", and the answer is the time, the gate, the terminal and the
belt, together. Same facts, arranged as the question:

    ↗ DFW · Dallas-Fort Worth International
      22:30ᶜᵀ  2̶2̶:̶2̶7̶ᶜᵀ                    [↗ B1]
                                        Terminal B
      ──── 1h 22m · 281 nm ──────────────────────
    ↘ XNA · Northwest Arkansas National
      23:52ᶜᵀ  2̶3̶:̶5̶1̶ᶜᵀ            [Bag 2] [↘ A1]
      1 min late                     Terminal Main

`.aptblock` lives in `static/app.css`, not in the template, because the
calendar's expandable strips (1.12.0) show exactly this for a flown leg.
See invariant 25.

**WORDS FOR LATENESS ON THE ARRIVAL ONLY** (owner's rule). The departure
keeps its tint and its struck-through original, so nothing is concealed —
it just is not narrated. A leg that pushed twelve minutes late and lands
on time is not a late flight, and printing "12 min late" under the
departure invites the family to read it as one. What they are asking is
when he gets there. One `showNote` flag on one function, not two code
paths, so the two ends cannot drift.

**The struck-through original appears here and nowhere else.** The
collapsed strip shows only the corrected time (1.9.0); the expanded view
is where there is room for what it moved from. `was_short`, added to the
payload in 1.9.0, is what made this drawable.

**"Closed out" removed** (owner's call). It named which internal RULE
ended the leg — `airline` / `relaunch` / `observed` / `backstop` — which
is diagnostics, and it sat directly above a row saying almost the same
thing in readable language. Still stored, still on the diagnostics page,
no longer on the family's card.

**"Arrival time from" now says it in English.** `airline` → "the
airline", `observed` → "our own tracking", `estimated` → "an estimate".
Translated in `view.py`, not in the template, so the page and the poll
cannot phrase it differently (P1-5). The three stay DISTINGUISHABLE
rather than being smoothed into one word: the logbook export (N3) may use
only airline-confirmed times, so the card must not present them as
interchangeable.

**Two new route facts, and why they are not measurements.** `_route_nm`
(great-circle between the two airports) and `_block_time` (from the two
RESOLVED UTC INSTANTS, never by subtracting one wall clock from the
other — that is the ANC-NRT bug of 1.1.0 in a different place). Invariant
9 blanks the LIVE figures without a position fix; these two are the same
before pushback, in the cruise and after closure, which is exactly why
they are safe to print beside figures that go blank. A test asserts a
LAX-JFK red-eye reads 5h 20m and not 8h.

**BUG: the panel had two disagreeing show conditions.** The template
gated it on `enriched`, the poller on `dep_line || arr_line || gates`. A
leg with a perfectly good scheduled time therefore rendered an empty
panel until the first poll arrived and the JavaScript overruled the
template. One condition now, asserted by test.

**Also removed:** the `<h3>Flight detail</h3>` heading (the panel opens
because the reader tapped "Flight details"; a heading repeating the
button that revealed it was the only text on the page doing that), and
`applyTimeLine`. The flight LIST still formats time lines the old way via
`window._ptTimeLineHTML` and is untouched — that is 1.11.0.

Tests: 1,040 → 1,067.

### 1.9.0 — one way to draw a flight

First of five UI chunks. This one builds the COMPONENT; the surfaces that
consume it follow. Nothing about polling, closure, matching, budget or the
schema changed.

**Three surfaces drew a flight three ways.** The tracker card stacked the
airport code above the time at each end of the progress track; the flight
list used a pair of arrow chips; the calendar agenda printed a hyphenated
range. Three implementations of one idea, drifting independently, with any
given fix landing on whichever one somebody happened to be looking at —
the same failure the palette had before v5.9 and the zone label had before
1.2.0.

`.fstrip` in `static/app.css` is now the only one, in three sizes:

    ENY3729                                    [In air]
    Dallas-Fort Worth to Oklahoma City
    (↗) DFW 20:10ᶜᵀ            (↘) OKC 21:32ᶜᵀ

`--lg` (tracker card), `--md` (tracker list, 1.10.0) and `--sm` (calendar,
1.12.0) override CUSTOM PROPERTIES ONLY, never layout rules — a test
asserts that, because a size modifier that restates a layout rule has
stopped being a modifier and become a fourth copy. Both arrow glyphs are
single shared partials carrying no width/height, sized from
`--fstrip-glyph`, for the reason invariant 22 exists.

**The colour rule, and why on time is not green.** Owner's call:

| | |
|---|---|
| green | EARLY |
| red | LATE, or cancelled |
| plain | exactly as scheduled, **or nothing published yet** |

Green has to mean "better than the plan" or it becomes the background
colour of the app and the eye stops reading it — the same reasoning that
says there is no "on time" pill. It also collapses a distinction the app
cannot otherwise draw at a glance: "the airline says on time" and "the
airline has said nothing" would look identical, and only one of them is a
report.

**The discs take their colour from their own time.** They shipped fixed by
direction (green out, red in), which is what the reference app does and
which meant a red disc could sit beside a green time and read as an
argument. The glyph already says which end it is, so direction does not
need the colour as well. A disc with nothing to report is drawn on
`--border` rather than left invisible (invariant 24). One function writes
the disc AND its time from one `line`, so the two cannot drift.

**The strip shows the corrected time and nothing else.** No struck-through
original, no "12 min late" chip. This is the always-visible summary; the
delta belongs in the expanded view, in words, where there is room (1.10.0).

**Zone superscripts on the card, and why the template was not the bug.**
Reported as the expanded card not using the superscript form 1.3.0
introduced. The cause was in the PAYLOAD, not the markup: `_time_line`
emitted one glued string, `"12:39 CDT"`, so the zone lived inside the same
text node as the digits and CSS had nothing to select. It now emits
`time_short`, `was_short` and `zone` alongside the glued `time`, which
every existing caller keeps using. Additive, so no API version bump. The
label is resolved against the leg's own scheduled instant, so it answers
daylight time for the day being shown rather than for today.

**BUG: tapping a flight blanked the two rows that matter.**
`applyEnrichment` hides Departure and Arrival when `dep_line`/`arr_line`
are absent, and `applyLegPayload` called it without them — so switching
legs wiped the expanded view's two most important rows until a full page
reload. Silent in the usual way: every other row on the panel was fine, so
it read as "still loading". Found while rewiring the card, not reported.

**Deleted, not merely unused:** `.flight-num`, `.city-route`,
`.route-ends`, `.route-end`, `.route-code`, `.route-time-wrap`,
`.chip-time`, `.chip-delay`, `.flight-line`. A test asserts each is gone,
matching on rule declarations rather than on the word, so the comment
recording the removal does not read as the removal not having happened.
`.route-strip` survives as the progress track alone.

Tests: 987 → 1,040. `tests_ui_fixes.py` 376 → 427.

### 1.8.0 — the diagnostics were the broken thing

**Feeds showed all red while tracking worked fine.** `probe()` called
`requests.get` directly instead of going through the throttle in
`livesource`, so opening the admin page fired one request per configured
feed with no spacing between them. adsb.fi allows 1 per second. Every feed
after the first came back 429, and the page reported them all dead. The
probe now shares the throttle, 429 is reported as rate limiting rather than
failure, and when the probe and the real lookup history disagree the
history wins — because the history is the answer to the question actually
being asked.

**"Active flights" listed legs that had closed hours ago,** and fired a live
uncached lookup at each one. `active_flights()` returns whatever is inside
the tracking window, and a leg stays there until 3 hours past scheduled
arrival whether it closed or not. The poller was always right —
`poll_once` skips closed legs — so this was the page misreporting the app,
and spending real ADS-B requests to do it. Closed legs are now listed
separately and not queried.

Both bugs were in the one place whose entire job is telling you whether
anything else is broken, which is the worst place for a bug to live.

**The decision log became usable.** Flight filtering was an exact match on
a full id like `2026-08-04-3729-DFW-OKC`, so it went unused; it is a
contains match now and `3729` works. Added: free-text search across
subject, event and detail together; an event menu built from what is
actually in the log rather than requiring you to know the names; a line
limit (100 default, was 200); a **live tail** that polls for what it has
not seen and stops when the tab is hidden; and **download** as plain text
carrying the same filters that are on screen.

**Admin page fits a phone.** The embedded diagnostics is generated markup
with its own inline styles — `white-space: nowrap` on every label and
180-character raw API errors in the values — so the whole page scrolled
sideways. The section is now width-constrained and long tokens break. Jump
pills removed.

**airplanes.live withdrew its free API.** See ADS-B FEEDS. Defaults are now
adsb.lol and adsb.fi; airplanes.live ships disabled with the way back
documented. Added a *reset feeds to defaults* button, because a saved list
in the database silently overrides the built-in one.

**docker-compose.yml: 60 lines to 25**, comments inline with the settings
they explain.

Tests: 948 → 987.

### 1.7.0 — saying what things are

Mostly correcting things 1.6.0 got wrong, which is worth recording as such.

**`/admin` is the install; `/flights` is your schedule.** The tab bar had
called the schedule page "Flights" since v7.5 while its URL said `/admin`,
and 1.6.0 then stacked the install's administration on top of it. Two
scopes, one page, a name matching neither. The schedule moved to
`/flights`; `/admin` is now people, test mode, diagnostics and the decision
log on one plain page. Diagnostics and the log were separate pages (one of
them unstyled and light-only, one reachable only by typing its URL) and are
now sections. Every moved URL keeps a redirect.

**Promotion needs your password.** A one-tap Make admin button was too
little friction for a grant that cannot be undone by the person who made
it.

**One aeroplane instead of three.** See ONE AEROPLANE. The map marker
looked bad for a specific reason — three overlapping shapes, each stroked
separately — and the progress-bar plane was painted the same colour as the
bar it rode on. New tile artwork: night sky, a great-circle arc split
solid-behind / dashed-ahead, and the plane banked along it.

**The hand-add flight form is gone.** It was never asked for: it was
inferred from a line in N1's own spec about a diversion that continued to
the original destination. Inventing UI from an inference is how a page
fills with things nobody wanted, and the parser plus a re-paste already
cover the real case.

**Timezone superscript down to 8px** from 9px.

Tests: 889 → 948. The tab bar is now one shared partial rather than four
copies, and its test asserts the partial and the routes that drive it
instead of scanning each template for markup.

### 1.6.0 — test mode, and a second admin

**Test mode.** Rehearse any scenario on demand, in minutes, for nothing.
The simulator produces position reports and nothing else; the app's own
tracking, tagging and closure code runs on them unchanged, so what you see
is what a real flight would do. Six scenarios, each named for the bug it
reproduces. Three isolation rules — never spend, never ask ADS-B, never
count — enforced at the boundaries rather than by convention. See TEST MODE.

The interesting design decision was rejecting a clock multiplier in favour
of **Age this leg**, which shifts recorded timestamps backwards. A
multiplier would have had the leg judged at one time and displayed at
another, and every resulting discrepancy would have been a property of the
simulator rather than of the app — a test harness that produces its own
bugs is worse than none.

**A second admin is now possible.** It was not before, which was a genuine
hole rather than a missing convenience: `create_user` set `is_admin` on the
first account and nothing else in the codebase ever touched the flag, so
losing that account lost administration of a self-hosted install for good.
`auth.set_admin` plus Make admin / Remove admin on the admin page, with a
last-admin guard that makes an unadministerable database unreachable.

**All administration moved to /admin.** The registered-pilots table and the
diagnostics link were in Settings; the decision log had no link at all. The
split was arbitrary — Settings is per-pilot preference, /admin is operating
the install — and on a shared install those are two different people.

Tests: 826 → 889, one new suite. Most of it is isolation: an invented
flight that leaked into a bill or a logbook would be worse than having no
test mode at all.

### 1.5.0 — the leg that would not let go, and N1

Four bugs, and the first three are the same bug wearing different hats: a
leg reaching the end of its life and nothing noticing.

**1. The abandonment cliff.** Reported as: blocked in at 07:00, still open
at 11:30. The 1.4.0 long-stop route was supposed to close that leg after 30
stationary minutes. It was correct and it could not run, because the leg
had stopped being swept. `active_flights()` returns the current leg and
imminent upcoming ones; `get_current_info` releases a leg 3 hours past its
SCHEDULED arrival. After that nothing looked at it again — while the
backstop was not due until 3 hours past the REVISED arrival, always later
on a late flight. Three of the five closure routes expired at that instant.

Fix: `poller._closeout_sweep`, re-judging unclosed rostered legs from the
last 7 days after each normal sweep. Costs nothing — every value closure
reads is already on the row.

**Why 714 tests missed it.** They drove `maybe_close` directly, or drove
`poll_once` over a 70-minute leg that finished well inside its window.
Nothing ran past the three-hour line, which is the only place the bug
lives. The lesson is not "write more tests"; it is that a rule and the
thing that CALLS the rule are two separate pieces of correctness, and the
suites only ever tested the first.

**2. The unreachable upgrade.** `maybe_close` could upgrade a provisional
close to the airline's real gate-in and never did, because a closed leg was
never polled and `should_query` refuses to spend on one. A door with a wall
behind it. Fix: `_gate_in_sweep`, three late attempts at +90 min / +6 h /
+18 h, capped and recorded before the call. This is the difference between
a logbook that can quote the airline's own figure and one that cannot.

**3. The handover.** Closing the leg turned out to be only half of it.
Selection asked which leg had STARTED, and a finished leg has still
started — so leg 1 held the card for the full three-hour grace while the
crew were boarding leg 2. Fix: `_on_ground`, deliberately broad (any of six
signals), handing over one leg at a time as soon as the next leg's window
opens. See WHICH LEG IS CURRENT for why broad is right here and wrong
everywhere else.

**4. N1 — additive import.** `save_schedule` replaced the roster, so
pasting September erased August. Renamed to `replace_schedule` and taken
off the import path; `merge_schedule` and `remove_legs` replace it there.
Reconciliation is scoped to the months the paste covers and to FUTURE legs
only, and every removal is proposed on a diff the pilot approves. A removed
past leg is deleted outright (owner's call). New `app/importer.py`.

Because the roster now accumulates, three surfaces had to grow up with it:
a server-side month filter on the flights page, a calendar that shows one
month at a time instead of stacking every month with data, and a per-leg
drop control on the review page beside the trip breaks.

Tests: 714 → 826, two new suites.

### 1.4.0 — the taxi-in trap, and a way to see why

**The bug.** A leg would stick on taxi-in forever and the next leg would
never become current. Cause: EVERY remaining closure route required the
transponder to go quiet. `observed` needed `signal_gap >= 8 min`; the
backstop tested `quiet` as well. An aircraft parked at the gate still
transmitting satisfies neither — and that is ordinary behaviour, not an
edge case. The only other exits were an airline gate-in, which is the OOOI
field most often absent, and `relaunch`, which on the last leg of a day
does not happen until the next morning.

**Fix:** a LONG STOP route. Landed, stationary for 30 minutes, closes the
leg regardless of signal. The five-minute-plus-silence pairing is retained
unchanged for the short case, and the `has_departed` guard still outranks
everything, so a delayed flight still cannot close itself at the gate. Five
regression tests in `tests_flight_row.py`, including the two negative cases
that keep the old behaviour honest.

**Why it took a code read to find, and what changed.** Nothing recorded WHY
a leg failed to close. The poller ran, the logic said "not yet", and that
decision left no trace. New `app/debuglog.py` records decisions with the
inputs that produced them — for closure, every threshold logged beside the
value it was compared against, so a near-miss is visible without opening
code. Read it at `/admin/debug`, filterable by flight id or event prefix.

Deliberate constraints: SQLite in the same database (already backed up,
already reachable from a phone); self-trimming at 20,000 rows, since the
poller would otherwise grow it without bound; retention NOT tied to
`PT_RETENTION_DAYS`, because flight history is precious and diagnostics are
disposable; never raises, because an app that dies over a debug row is
worse than one with no debug rows; and key-like fields are redacted on the
way in as a backstop to the rule of not passing secrets at all.

**Off by default** (`PT_DEBUG_LOG=1`). It writes a row per poll and is only
worth the writes while chasing something.

**Progress strip hides when there is no progress.** It used to render an
empty track at 0% whenever there was no position fix, which reads as a
broken bar rather than as absent information. Now gated on a real fix, or
on the leg having arrived — where a full bar means something. The live poll
applies the same rule; without that the strip would render hidden and be
un-hidden, empty, on the first refresh.

**A brittle test corrected.** `tests_ui_fixes.py` asserted the template
count equalled ten, which broke the moment a page was added. What it meant
was that `viewer_settings.html` stayed merged into `settings.html`; it now
asserts that instead.

Tests: 680 -> **714**.


### 1.3.1 — the plan, written down

Documentation only. No code changed, so no test count moved.

Added `## NEXT UP`, recording the five committed features (additive import,
logbook view, CSV export, named invites, viewer-side framing), the order
they go in, and WHY that order is a dependency chain rather than a
preference: everything after N1 assumes flights accumulate instead of
rolling over, so building any of the others first means building it twice.

Each entry carries the design decisions already settled in discussion, so
they do not have to be re-litigated or, worse, silently re-decided:

- Import runs by MONTH and prunes only FUTURE legs; history is not
  revisable by a paste.
- Nothing applies silently — `import_review.html` becomes an approved diff.
- Leg CONFIRMATION gates the logbook export. This is what makes diversions
  tractable without the app having to infer anything, and it bounds the
  AeroAPI backfill.
- The export uses `*_actual_api` values only. A missing value leaves the
  row incomplete rather than being filled in from observation — a derived
  time must never masquerade as a reported one in a legal record.
- ONE database, three views. An earlier misreading of "separate views" as
  "separate databases" is corrected in place, since splitting the database
  would break the shared flight row design.
- The pilot/viewer template split happens WITH N5, not before, and P0-6
  (moving inline JavaScript out of viewer.html) lands immediately before
  it.

Also recorded what the sequence deliberately omits — native client work,
push delivery, and anything trigger-gated — so a future session does not
read their absence as an oversight.


### 1.3.0 — the zone as a superscript

Follow-up to 1.2.0, closing the reason the inconsistency existed at all.

**Why the old rule existed.** Zones were suppressed when departure and
arrival matched because a full-size label beside every time was wide enough
to wrap a row on a phone. The economy was real; the cost was that three
surfaces implemented it three different ways.

**The fix removes the tradeoff instead of picking a side.** `.tz` is now a
superscript at 0.5625rem. Two letters cost a couple of millimetres, so
every time can carry its own label and the layout stops caring. Consistent
AND compact.

Lifted with `transform: translateY`, not `vertical-align: super` — the
latter grows the line box and spaces out every row it appears in.

**Newfoundland.** Generating a label for every airport in the realistic
network (US, Canada, Mexico, Central America, Caribbean) and listing
whatever failed to collapse to two letters turned up exactly one gap:
`NST`/`NDT` for America/St_Johns. Added. Every zone in that network now
reads as two letters, and a test asserts it airport by airport.

**Accessibility.** The superscript is decoration for someone scanning a
column of times; a screen reader announcing "C T" after each one is noise.
The span carries `aria-hidden="true"` and the time itself stays readable.
The first attempt put `aria-hidden` in the CSS, where it does nothing —
a test now fails on that specific mistake.

Tests: 654 -> **680**.


### 1.2.0 — one zone label, one rule

1.1.0 fixed timezone CORRECTNESS. This fixes what was actually reported:
labels that looked inconsistent from screen to screen.

**Three producers, three answers.** `view._fmt_local`, `main.fmt_local` and
`main._fmt_utc_local` each built zone labels independently and disagreed on
both things that matter:

- `main.fmt_local` and `main.tz_abbr` derived the label from a HARD-CODED
  sample date of `2026-07-01`, so every label they produced was the SUMMER
  one. Invisible in North America, since CDT and CST both collapse to "CT".
  Wrong everywhere else: a January London leg was labelled BST.
- The fallbacks differed. One rendered `tz_name.split('/')[-1]`, putting
  the CITY name — "Chicago", "Phoenix" — where a two-letter zone belonged.
  Another fell back to an empty string. That single line explains most of
  the reported "some are 3 letter, some are 2".

All three now call `view.zone_label(tz_name, on)`, which takes the date
being displayed and answers daylight time for that day. `tests_timezones.py`
fails the build if `tzname()` is called anywhere outside it, or if a
hard-coded sample date reappears.

**Three display rules on one screen.** In `viewer.html`, the flight list
showed the arrival zone always but the departure only when the two
differed, while the current-flight card showed NEITHER when they matched.
Reported as "some after both times, some after just the second time" —
which was exactly right.

Now: **every time carries its own zone, everywhere.** The "state it once
where you can" economy was not worth it. A family member does not know that
a missing label means "same as the other one"; they just see a gap.

The compact-RANGE surfaces (calendar agenda, import review, admin) keep one
suffix for the pair — `CT`, or `CT/MT` when the ends differ. That is a
different layout, not a different rule, and it is now the only permitted
alternative.

Tests: 641 -> **654**.


### 1.1.0 — timezones, actually fixed

The owner reported repeatedly that times were wrong and that previous
attempts had been bandaids. He was right. Three separate bugs, all SILENT —
nothing raised, nothing logged, no leg vanished. The times were just wrong,
and the only detection mechanism was a pilot looking at a schedule and
saying "that isn't right".

**Bug 1 — nonexistent local times.** On a spring-forward date the wall clock
jumps 0200 to 0300, so 0230 never happens. `datetime.combine(d, t,
tzinfo=tz)` accepted it and produced an instant anyway, with no signal.

**Bug 2 — ambiguous local times.** On a fall-back date 0100-0159 happens
twice. Python's default `fold=0` silently picked the first. Correct, as it
turns out, but by inheritance rather than by decision — and untested.

**Bug 3 — the bandaid, and the real one.** Arrival dates were inferred with
`if arr_time_local < dep_time_local: add a day`. That compares a clock in
the ORIGIN's zone against a clock in the DESTINATION's zone as though they
were one clock. The code's own comment called it a "simple heuristic". It is
right for most domestic legs BY LUCK and wrong outright once offsets differ
enough: an ANC-NRT leg lost a full day, because 1700 reads later than 1400
and the condition never fired.

**The fix.** New module `app/timezones.py` owns every wall-clock-to-instant
conversion. Arrival dates are no longer inferred from clock arithmetic at
all — departure is resolved to a real instant, then each candidate arrival
DATE is tested and the first one landing after departure inside a believable
block (20h ceiling) wins. That is correct for every zone pair including the
date line, with no special cases and no heuristic.

DST edges are now handled by DECISION rather than default, and documented:
a nonexistent time resolves FORWARD past the gap (0230 becomes 0330, never
0130 — an hour early is the direction that makes a crew member miss a
report), and an ambiguous time takes the first occurrence, which is what a
published schedule means.

**A latent crash fixed on the way past.** `parser.py`'s sort fallback built
a NAIVE datetime while resolved legs produced aware ones. Python refuses to
compare the two, so any paste mixing known and unknown airports would have
raised and lost the entire import. Never reported, presumably because every
airport in the owner's flying resolves.

**New INVARIANT, enforced by test:** nothing outside `app/timezones.py` may
build a UTC instant from a named zone. `tests_timezones.py` greps the app
package for `datetime.combine(..., tzinfo=<named zone>)` and fails on any
hit. `tzinfo=timezone.utc` is exempt — there is no wall clock there and no
DST to get wrong.

**Why this landed before the logbook.** A logbook export is a legal record
built from these instants. Shipping the export first would have written an
hour-wrong OOOI time into a real pilot logbook twice a year, and a wrong
block time on anything crossing enough zones. Order matters here.

Tests: 609 -> **641**, seven suites -> eight.


### 1.0.0 — the rebrand, the install shell, and a year of memory

Renamed **Pilot Tracker → MyPilot**, and restarted the version numbers.

**Why the numbers restarted.** The old scheme was a single decimal: 5.5,
6.3, 7.4. After 7.9 comes 7.10, which sorts BEFORE 7.9 as text and equals
7.1 as a number — so anything that ever compares versions reads the newer
build as older, and no later fix can disambiguate the numbers themselves.
1.0.0 rather than 0.1.0 because the app already flies real trips for real
families with hundreds of tests behind it. See VERSIONING.

**Made it an app rather than a website that resembles one.**
- Service worker (`static/sw.js`), served from `/sw.js` — a worker under
  `/static/` can only ever control `/static/` and would never see a page
  navigation or an API call. Cache name keyed to VERSION, so every deploy
  rotates it; without that, `update.sh` silently stops reaching phones.
- The install shell moved into `templates/partials/app_shell.html` and is
  included by ALL TEN templates. It was previously on two. The login page —
  the first screen any family member sees — had no manifest at all, so the
  install that mattered most produced a bare bookmark.
- Manifest is now a route, generated per user so the icon choice applies.
- `theme_color` corrected from `#1e3a8a` to `#0f1419`; it disagreed with the
  background and flashed blue on launch.
- Connection banner distinguishing offline / can't-reach-server / showing-
  saved-data. Driven by actual poll outcomes, NOT `navigator.onLine`, which
  reports true on captive portals and while the server is down — precisely
  the airport-wifi case a family member hits.

**New icon, and a picker.** Four plane silhouettes, generated by
`make_icons.py` from one set of vector definitions and emitted into
`static/planes.js`, so the map marker and the app icon are cut from the same
shape and cannot drift. Selectable in settings. Caveat stated in the UI: a
home-screen icon already installed does not change until reinstall, because
both iOS and Android read the manifest once at install time.

**Retention 30 → 365 days.** `flights.py` and `track.py` together — a track
outliving its flight row, or the reverse, is how half-deleted legs happen.
`PT_RETENTION_DAYS` overrides. **This is the release where the database
stopped being disposable**, hence BACKUP.md.

**Future-proofing for clients we cannot reach.** All of it cheap now and
expensive-to-impossible later; none of it needed today:
- `/api/v1/…` with the bare paths kept alive as aliases.
- `/api/v1/meta` reporting build, API version and whether a caller is still
  supported, so an old native client can say "please update" instead of
  rendering blank.
- `SCHEMA_VERSION` stamped into `app_meta`, with a hard refusal to start
  against a database newer than the build. Rolling back to an older image is
  the classic way to lose data quietly.
- Carrier callsigns moved to `app/carriers.py` as configuration
  (`PT_HOME_CALLSIGN`, `PT_CANDIDATE_CALLSIGNS`). They were NOT deleted with
  the rest of the branding — deleting them breaks deadhead resolution
  outright. The invariant that the home prefix leads the candidate list is
  enforced in code, not assumed, because a typo in compose would otherwise
  make every one of the pilot's own legs unresolvable and silently so.

**Housekeeping.** `data/secret_key.txt` had been shipped inside the v7.4 zip
despite this file forbidding it. The packaging step now checks the archive
rather than the working tree, since a test run recreates both.

Tests: 400 → **609**, six suites → seven (`tests_app_shell.py`). The 400
figure in the old README was itself stale; the v7.4 tree already had 472.


### v6.4 – v7.4 — NOT RECORDED

Four versions shipped without entries. Reconstruct from `git log` and add
them; until then this history is incomplete and the STATE warning applies.

Known to have landed somewhere in this range, by inspection of the v7.4
tree: the shared `static/app.css` palette (with the `data-theme` /
`prefers-color-scheme` precedence rule), light theme on the auth pages,
and the `<nav class="tabbar">` bottom bar on all four logged-in pages.
Rationale for each is unrecorded — recover it before changing any of them.

### v6.3 - three separate reasons the phone looked broken

- **BUG: the map drew nothing on mobile.** It is sized by a FIXED,
  full-viewport parent, and this script runs while the page is still laying
  out. Mobile Safari routinely measures that box as 0x0 at that instant, so
  Leaflet cached zero dimensions and never requested a tile — blank on
  phones, fine on desktop, which resolves layout early enough to get away
  with it. Before v6.1 the map had an explicit pixel height and the
  question could not arise. Now `invalidateSize()` runs after two animation
  frames, on `load`, on `orientationchange`, and through a `ResizeObserver`
  on the map element so the iOS URL bar collapsing or a rotation re-measures
  too.
- **BUG: black input boxes on the logged-out pages.** All five hardcoded
  `background: #0f1419`. Invisible while those pages were dark regardless
  of preference; the moment v5.9 made them follow the system theme, a
  light-mode user got a white page with black fields. Now `var(--input-bg)`.
  A test forbids any dark-palette hex in any template.
- **Pages are no longer cacheable.** Every asset URL carries `?v=VERSION`,
  so assets can be cached hard and a new build asks for new names. The HTML
  had no such handle and the browser decided for itself — mobile Safari
  will hand back a page from before the last deploy. That made a fixed bug
  look unfixed on one device and fixed on another. HTML now sends
  `no-store`; static assets are untouched and still cache.

**Diagnosing "it works on desktop but not my phone":** compare the version
in the footer on both. If they disagree, the phone is running old markup
and the answer is cache, not code.

### v6.2 - one missing file should not cost the whole page

Reported as "the map is not visible at all and the scroll/fade is not
working". Those were one fault, not two.

- **The map and the reveal shared a fate.** The scroll reveal was written
  inside the same IIFE as the map setup. `L.map(...)` is the third line of
  that block, so if `leaflet.js` does not load, `L` is undefined, that line
  throws, and EVERYTHING after it in the block never runs — including the
  reveal. One absent file, and the page lost its map and its schedule
  together. The reveal is now its own top-level block, placed BEFORE the
  map block, and the map block opens with a `typeof L === 'undefined'`
  guard that logs, shows "Map unavailable" in place of a silent blank, and
  returns. Now invariant 16.
- **CSS must not hide what only script can restore.** `.reveal` shipped as
  `opacity: 0` with the script expected to animate it back, so a dead
  script meant an invisible schedule. It now defaults to visible and the
  script sets the starting opacity itself, once it is known to be running.
  Verified by executing the page's JS against a DOM stub with no Leaflet
  present: no throw, schedule at full opacity, fallback message shown.
- **No map means no reveal.** With nothing behind it to reveal, the effect
  is just a way of hiding the schedule, so the map's failure path calls
  `_ptRevealOff()` and hands the list back at full opacity.
- Nav pills widened (56px -> 76px minimum) and dropped closer to the
  bottom edge; page padding trimmed to match.

**Deploy note:** the likely trigger was `static/vendor/` not reaching the
repo — browser folder uploads routinely drop nested directories. If the map
is missing, check `/static/vendor/leaflet/leaflet.js` returns a file rather
than a 404 before looking anywhere else.

### v6.1 - the map really is the background now

- **BUG: settings could not be saved.** The spend limit input carried
  `step="0.25"`, so the browser only accepted multiples of a quarter. The
  DEFAULT budget is 4.90. A fresh account therefore could not save ANY
  setting — theme, time format, poll interval, nothing — because the form
  failed validation on a value the app itself had put in the box, and the
  browser blamed the user with "please enter a valid value". Now steps in
  cents. A test asserts the default would have failed the old step, so the
  pairing can never silently drift apart again.
- **v6.0's full bleed was not full bleed.** The map was still a block in
  the flow with negative margins faking it, ending partway down the page.
  It is now a FIXED backdrop covering the entire viewport, top to bottom,
  with the page scrolling over it. `isolation: isolate` confines Leaflet's
  panes (which climb to z-index 800) instead of the z-index juggling v6.0
  used.
- **The gradient over the topbar is gone.** It was covering the top of the
  map layer and recentre buttons. The brand keeps its position and gets a
  text halo, which is legible over any map without covering anything.
- **Scroll reveal.** At rest you see the map, the live flight card and the
  nav pill. Scrolling fades a scrim in over the map and the schedule up
  from nothing, tracking the finger via `requestAnimationFrame` rather than
  a timed transition. The list is `pointer-events: none` while effectively
  invisible so a stray tap cannot land on a row nobody can see, and carries
  `min-height: 72vh` because a one-leg day would otherwise leave the page
  too short to scroll — the schedule could never be revealed at all.
  `prefers-reduced-motion` skips the whole effect.
- **The tab bar is a floating pill, offset left,** rather than a full-width
  bar that would have cut the map in half now that it reaches the bottom
  edge.
- **Sign out now appears only on Settings,** in the same place it was.
- **"All times CT" removed from the card.** Cross-zone legs still label
  each end, which is the case that carries information.
- **Row zone labels moved next to their times.** `margin-left: auto` had
  stranded them at the far right of the row, in space, away from the number
  they described. A same-zone leg now states it once on the arrival; a
  crossing states it at both ends.
- **Show on map is back, as an explicit action** in each row's detail panel.
  A bare tap on a row still expands in place and deliberately does not move
  the card or map — that was v5.4's fix for losing sight of the live flight
  while checking last Tuesday's gate — so this is an added action rather
  than a reversal. It selects the leg and scrolls back up to the map.
- `tests_ui_fixes.py` 179 → 199.

### v6.0 - it stops looking like a web page

- **Nothing render-blocking is remote any more** (invariant 15). Leaflet
  loaded from `unpkg.com` in the `<head>` with no `defer`, so the browser
  drew NOTHING until a public CDN answered. That is a white screen caused
  by a third party while this server is healthy, and it is indistinguishable
  from the app being down. Leaflet 1.9.4 and Sortable 1.15.2 are now under
  `static/vendor/`, pulled from the npm registry and version-matched to
  what the tags requested. Leaflet's CSS references its marker images
  relatively, so `images/` sits beside it — a test asserts this, because
  getting it wrong makes markers vanish silently.
- **Navigation moved to the bottom.** `.topnav` was a strip of small text
  links at the top beside the logout button: the hardest part of a phone to
  reach, and it wrapped to two lines on a narrow screen. Now a fixed bottom
  tab bar with icons, in `app.css` so all four pages share one definition,
  padded for the home indicator. Viewers get three tabs; Flights is behind
  `is_pilot`.
- **The map is full-bleed.** Negative margins cancel the body padding so it
  reaches the physical screen edges and runs up under the status bar, with
  square corners — a rounded rectangle reads as a picture placed on a page,
  which was the impression being removed. The card overlaps its bottom edge
  by 2.25rem so the two read as one surface. The topbar keeps only the
  brand and logout, sits at `z-index: 600` with a scrim that fades from
  `var(--bg)` (theme-agnostic, so it needs no knowledge of which theme is
  live). `.map-wrap` carries `z-index: 1` to confine Leaflet's own stacking,
  whose panes climb to 800 and would otherwise paint over the topbar.
- **Zones are two letters: CT, MT, PT, ET, AKT, HT, AT.** Owner's idea was
  a superscript ±offset; that notation is already taken in aviation, where
  a small +1 beside an arrival means it lands the NEXT DAY — overloading it
  in a crew app would be actively misleading, and "+2 relative to what?"
  has no obvious answer. Two-letter labels are shorter, are what consumer
  apps use, and **retire the fixed-July-sample bug rather than fixing it**:
  a label that never claims daylight or standard time cannot be wrong about
  which is in force, so a December leg reading CDT is no longer possible.
  Phoenix is the loose end — Arizona skips daylight time, so MT is a broad
  name for it, still correct as a zone and no worse than the MST it showed.
  Anything outside North America keeps whatever the zone database calls it.
- `tests_ui_fixes.py` 124 → 179.

### v5.10 - finishing what v5.9 started

v5.9 claimed the zone rule was applied everywhere. It was applied to the
tracker card, the flight list and the calendar agenda, and not to the two
pilot-facing pages.

- **Flights table and import review follow the zone rule now.** Both still
  called `fmt_local()` with the zone glued on. The Flights table is six
  columns already needing a horizontal scroll on a phone, and two of them
  were half again as wide for no information, since almost every leg starts
  and ends in the same zone. Zone is its own column: one abbreviation when
  both ends match, `CDT/MST` when they don't.
- **The Flights table printed raw ISO dates** (`2026-08-15`) while the
  import review two clicks earlier said `August 15`. Now `Aug 15`. The ISO
  form is kept as `date_iso` purely for the delete confirmation, where
  ambiguity would be expensive.
- **"12 leg(s) loaded"** now pluralises properly.
- Nine new assertions, including one that had to be narrowed: the first
  version asserted no `"date": str(leg.date)` anywhere in `main.py`, which
  also caught `leg_view`'s date — that one is machine-readable, used for
  grouping and anchors, and should stay ISO.

### v5.9 - one palette, one settings page, zones that fit

- **One stylesheet.** `static/app.css` now owns the colour variables and
  every template links it. All eleven carried a private copy; they had
  already drifted (`--border` was `#2a3548` on the tracker, `#334155` on
  settings and admin), and a colour change meant eleven edits.
- **Light mode now works everywhere.** Five pages — login, register, setup,
  forgot_password, recovery_code — declared only the dark block and had no
  `data-theme` attribute, so a light-mode user got a black login then a
  white app. They have no account to read a preference from, so they follow
  `prefers-color-scheme`. The `:root:not([data-theme])` guard is
  load-bearing: without it a pilot who chose dark would be overridden by
  their phone being in light mode. Settings itself was also dark-only, so
  choosing "light" there did nothing to the page you chose it on.
- **BUG: `--input-bg` was only ever declared inside settings.html's light
  block,** while admin.html used it too. Text inputs on both pages fell
  back to transparent in dark mode. Now in the shared palette, both themes.
- **Settings is one page.** `viewer_settings.html` is deleted. Pilot-only
  sections (AeroAPI key and budget, poll interval, account recovery) sit
  behind `{% if is_pilot %}`; the roster needs `is_pilot and is_admin`.
  Storage still differs and should — a pilot's settings are in the database
  and follow the account, a viewer's are a cookie on their device — so only
  the form's action changes. The two templates had already drifted: the
  pilot form named its checkbox `show_flightaware`, the viewer form
  `show_fa`. The viewer route now takes the pilot name; the COOKIE name is
  unchanged, so nobody's saved preference resets on upgrade.
- **Zone labels stopped wrapping times.** `fmt_local` glues the zone onto
  the time and returns one string, so on a phone a departure read "7:00 AM"
  on one line and "CDT" on the next, twice per row. New `tz_abbr()` plus
  `dep_zone` / `arr_zone` / `same_zone` on the leg payload let templates
  place the two separately. The rule, applied on the card, the flight list
  and the calendar agenda alike: **a leg that starts and ends in the same
  zone states it once; a leg that crosses one states it at both ends**,
  which is the only time it carries information.
- **The tap-to-reveal zone bubble is gone.** With zones on screen there was
  nothing left to reveal, and the owner's objection was right — a tap
  target with no affordance is undiscoverable, and it left the card and the
  list stating zones by two different rules. Roughly 40 lines of JS and CSS
  removed. The expanded detail rows keep the full "04:13 CDT" form: they
  are one value per line with room, and that is where a revised time
  matters most.
- **Registration says who it is for** — "For crew only", and a line
  pointing a family member at the tracking code instead. "(optional, for
  later)" dropped from the email label; the field is still optional.
- `tests_ui_fixes.py` 72 → 115: no template may declare a palette, every
  template links the stylesheet, both themes define every variable,
  logged-out pages follow the system, the settings gating, and the zone
  layout including that no trace of the old bubble survives.

**Known, not fixed:** `tz_abbr` samples July, so abbreviations read as
daylight time year-round — a December leg says CDT where it should say CST.
Pre-existing in `fmt_local`; fixing it means resolving each leg's zone
against its own date. Logged in OPEN.

### v5.8 - "today" is a local day

- **BUG: the calendar highlighted tomorrow every evening.** `calendar_page`
  did `now = datetime.now(ZoneInfo("UTC"))` and then `today = now.date()`.
  An INSTANT is fine in UTC and compares correctly against anything; a
  CALENDAR DAY is not, because turning an instant into a date needs a zone.
  From 19:00 Central onward, UTC is already tomorrow, so the grid ringed
  the wrong cell and the agenda anchor pointed a day ahead. Reported from a
  screenshot stamped 23:19 local showing the 13th on the 12th. Fixed with
  `now.astimezone().date()`, which converts to the container zone that
  docker-compose already pins via `TZ` (America/Chicago by default). The
  three other UTC clocks in `main.py` are instants, are correct, and were
  left alone.
- **A latent flaky test, found by the same clock.** `test_flight_list` built
  its "yesterday" leg 1400 minutes back from the real time, which only lands
  on yesterday if the suite runs before roughly 23:20. It failed once, at
  23:34, during this session. Now anchored to midday local so every offset
  stays in the day it was written for.

### v5.7 - honest figures, and flights that outlive a roster

Owner's call on both. No schema change, no migration, no UI restructuring.

- **ETE was the last thing still running on the clock.** Progress and
  distance were already position-derived; `compute_remaining_minutes` fell
  back to counting down to the airline's revised arrival whenever there was
  no fix or the aircraft was below taxi speed. On a coverage hole mid-cruise
  the percentage and the distance correctly vanished while "ETE 21 min"
  stayed lit beside them, ticking against a timetable — one figure on the
  card contradicting the two blanks next to it. Fallback removed; the
  `revised_arrival` / `now` parameters went with it, and `view.py` no longer
  computes a revised arrival for this purpose. The airline's estimate is
  still shown, on the Arrival line, labelled as an estimate.
- **Progress returns None before departure** instead of a pinned 0.0. Same
  category of problem, smaller: a zero looks measured and isn't. It only
  became visible when the figure moved onto the always-on-screen card in
  v5.6, where a parked aeroplane icon at the origin reads as "tracking"
  rather than "hasn't gone anywhere yet". The `departed` gate itself stays
  and is protective, not cosmetic — the hex lock can point at an airframe
  still inbound on its own previous leg, so a fix taken before pushback can
  belong to an aeroplane halfway across the state.
- **The three figures render together or not at all,** server-side and in
  `applyProgress`.
- **Retention is now the only thing that deletes a flight** (invariant 14).
  `purge_old` also ran `DELETE FROM flights WHERE id NOT IN (SELECT
  flight_id FROM roster)`. The owner imports throwaway schedules to watch
  live traffic; that un-rosters every real leg, and the next sweep — every
  6 hours, and on the first tick after container start, so any `update.sh`
  triggered it — deleted them outright. Gates, actual times, closeout, all
  of it. Re-pasting the real bid line then found no row to adopt and built a
  blank one from the timetable; the tracks died on the sweep after that.
  Diagnosed by running the sequence against a scratch database, not by
  reading. Cleanup was also reordered so a row and its dependent rows go in
  the SAME sweep rather than one apart. Un-rostered flights now age out at
  `RETENTION_DAYS` like everything else and cost nothing meanwhile:
  `active_flights()` walks each user's schedule, never this table, so a
  retained row is never polled and never triggers an AeroAPI query.
- Consequence, and the point of the change: an FO importing a bid line for
  a trip already flown adopts the existing rows and immediately sees the
  gates, times, aircraft and track from when they flew it together.
- `tests_flight_row.py` 50 → 63: the swap-schedule-and-restore sequence end
  to end, the FO adoption case, un-rostered rows still obeying the 30-day
  cutoff, and assertions that no revised arrival can manufacture an ETE.

### v5.6 - the route strip

UI only. No change to the poller, closure, matching, budget, schema or any
invariant. `viewer.html` is the only template touched.

- **The route strip replaces three homes for one fact.** How far along the
  flight is lived in a percentage in the map corner, a progress bar hidden
  inside the collapsed detail, and nothing at all on the part of the card
  that is always on screen. Now: origin and destination at the ends of a
  line, the aeroplane on it, `% en route / nm to go / ETE` under it, all
  above the fold. The TRACK always draws, because it is the route. The FILL
  and the plane move only on a real position fix — invariant 9 is intact,
  and `route-plane-el` is hidden outright when `progress_pct` is null.
  One deliberate addition: a leg whose phase is **Arrived** fills to the far
  end, on closure's authority, and still prints no percentage. Nothing is
  derived from elapsed clock time.
- **The schedule is no longer behind a tap.** `Tap for more` toggled the
  card's detail AND `#expand-wrap`, so on first load a viewer saw a map, one
  card and a 0.72rem grey line — the trip itself was invisible until they
  found it. `setExpanded` no longer touches the wrapper; the disclosure is
  now a proper 40px row inside the card and opens only the card's own detail.
- **BUG: `applyEnrichment` was called from inside the progress branch,**
  three conditionals deep in `refreshLiveData`, so gates, revised times,
  the airline-data age line and (since v5.5) the live row's pills only
  repainted while the aircraft had a live fix. A delayed leg at a gate with
  no ADS-B coverage — a small outstation, exactly when the family refreshes
  hardest — showed nothing new until a manual reload. Diagnosed by reading
  the nesting, not by observation; it needs an enrichment update to arrive
  during a coverage hole to show itself. Hoisted to the top level beside
  `applyPills`, with `applyProgress` alongside it.
- **Progress is rounded at display time.** `track.py` keeps one decimal,
  correctly. The card was printing "62.3% en route / 48.4 nm to go", which
  was tolerable inside a collapsed panel and is not on the hero card.
  Rounded in the template and in `applyProgress` only — `track.py`, the
  columns and the tests are untouched.
- Polish: tabular figures on every time and number, so digits stop shifting
  on each poll; list-row times promoted from muted to full contrast with the
  route demoted under them; the grey disc icons on list rows replaced by
  plain glyphs; the card's 2px accent border reduced to a hairline so the
  live-row marker is the only accent left; fourteen font sizes reduced
  toward six; a pulsing skeleton instead of the word "Loading"; keyboard
  support and `prefers-reduced-motion` on the new controls.
- Removed dead CSS: `.card.current` (never applied to anything),
  `.times-row` / `.time-chip` / `.chip-body` / `.chip-line` / `.chip-icon` /
  `.chip-code`, and the old `.progress-*` bar the strip replaced.
- `tests_ui_fixes.py` gains a template audit (17 assertions), including a
  check that the three poll-path functions sit at the top level of the
  handler — that is the bug above, made loud.

**Still open from the brainstorm, deliberately not done here:** bottom tab
bar; one shared stylesheet (eleven templates each carry a private copy of
the palette and they already disagree on `--border`); light theme on
`login` / `register` / `setup` / `forgot_password` / `recovery_code`, which
are hardcoded dark; manifest `theme_color` (#1e3a8a) not matching the app
background (#0f1419); manifest linked from only 2 of 11 pages; animating
the map's plane marker between polls.

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
