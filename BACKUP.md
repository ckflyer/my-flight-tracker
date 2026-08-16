# Backing up MyPilot

## Why this file exists now and did not before

Until v1.0.0 the database held a rolling 30-day window. Losing it cost you a
month, and the next month refilled it. Retention is now **365 days**, which
changes what the file is: `data/flighttracker.db` is the only copy of every
track flown, every actual gate time, and every closeout record for a year.
Nothing regenerates it. AeroAPI will not re-sell you a flight from March, and
the ADS-B breadcrumbs were never anybody's data but yours.

So the database went from disposable to irreplaceable in one release, and
that is the entire reason for this document.

## What to back up

Everything lives in one directory — `data/`:

| File | What it is | Replaceable? |
|---|---|---|
| `flighttracker.db` | accounts, schedules, flights, tracks, closeouts | **No** |
| `flighttracker.db-wal`, `-shm` | in-flight writes not yet folded into the main file | No, while running |
| `secret_key.txt` | signs session cookies, if you pinned one | Yes, but everyone gets logged out |

Nothing else needs backing up. The code is in GitHub and the container
rebuilds from it.

## The one thing people get wrong

**Do not copy `flighttracker.db` with `cp` while the app is running.**

SQLite runs in WAL mode here, which means recent writes live in the `-wal`
file and have not yet been folded into the main database. A plain copy takes
the main file at whatever instant it was read, misses the WAL, and produces a
file that opens fine and is quietly missing or corrupting the most recent
data. It looks like a successful backup right up until you need it.

Use SQLite's own backup command instead. It takes a consistent snapshot
while the app keeps running, with no downtime:

```bash
sqlite3 /path/to/data/flighttracker.db ".backup '/path/to/backups/mypilot-$(date +%F).db'"
```

That is the whole technique. Everything below is scheduling and storage.

## Manual backup, right now

From the host, with the container running:

```bash
cd /path/to/flight-tracker
mkdir -p backups
sqlite3 data/flighttracker.db ".backup 'backups/mypilot-$(date +%F).db'"
gzip -f backups/mypilot-$(date +%F).db
```

A year of data compresses to a few megabytes. You can keep every daily
backup for years without noticing.

## Scheduling it

Whatever runs it, the command is the same. Two reasonable options on
TrueNAS:

**A cron job on the host.** Daily at 03:00, keeping 30 days:

```bash
0 3 * * * cd /path/to/flight-tracker && sqlite3 data/flighttracker.db ".backup 'backups/mypilot-$(date +\%F).db'" && gzip -f backups/mypilot-$(date +\%F).db && find backups -name '*.db.gz' -mtime +30 -delete
```

Note the escaped `\%` — cron treats a bare `%` as a newline and the command
will fail confusingly without it.

**A TrueNAS ZFS snapshot task** on the dataset holding `data/`. This is the
better answer if it is available to you: snapshots are atomic at the
filesystem level, cost almost nothing, and cover the WAL and the main file
together. Set it on the dataset, not on the whole pool.

Belt and braces is fine. They protect against different things — a snapshot
protects against the disk and the container, a `.backup` file protects
against the pool.

## Before any deploy that touches the schema

Take one by hand first:

```bash
sqlite3 data/flighttracker.db ".backup 'backups/pre-deploy-$(date +%F-%H%M).db'"
```

The app now refuses to start if it finds a database newer than the build
(see `_stamp_schema_version` in `app/db.py`), which protects you from the
worst case — an old image quietly writing rows that drop new columns. But
refusing to start is a guard, not a recovery. The backup is the recovery.

`app/version.py` says which kind of release you are deploying:
**MAJOR** changes migrate data one way and cannot go back — always back up.
**MINOR** and **PATCH** are far safer, but this takes ten seconds.

## Restoring

Stop the container first. Restoring underneath a running app produces
exactly the corruption you were protecting against.

```bash
docker compose down
cd /path/to/flight-tracker
gunzip -c backups/mypilot-2026-08-15.db.gz > data/flighttracker.db
rm -f data/flighttracker.db-wal data/flighttracker.db-shm
docker compose up -d
docker compose logs --tail=30
```

Deleting the stale `-wal` and `-shm` matters. They belong to the database you
just replaced, and leaving them alongside a restored file is how a good
backup still ends in a corrupt database.

## Verifying a backup is real

An untested backup is a rumour. Once, after setting this up:

```bash
sqlite3 backups/mypilot-2026-08-15.db "PRAGMA integrity_check;"
sqlite3 backups/mypilot-2026-08-15.db "SELECT COUNT(*) FROM flights;"
sqlite3 backups/mypilot-2026-08-15.db "SELECT COUNT(*) FROM positions;"
```

`integrity_check` should print `ok`. The two counts should look like a year
of flying rather than zero. If a count is zero, you copied the file while the
app was running.

## A note for when this is hosted for other people

Everything above is a single-operator procedure and is fine while MyPilot
runs on your hardware for your crew. The moment other families depend on it
(roadmap trigger T1), backups stop being a personal precaution and become an
obligation to them: keep a copy on different hardware in a different
building, and know your restore time before you need it.
