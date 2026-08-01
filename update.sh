#!/bin/bash
# Pull the latest code and force Docker to rebuild the image.
#
# Why this exists: Dockge's Deploy/Update button runs the equivalent of
# `docker compose up -d`, which reuses the cached image if one already
# exists — it will NOT rebuild just because the Dockerfile or source
# changed. This script does what the button doesn't.
#
# Uses `fetch` + `reset --hard` instead of `git pull`, so a stray local
# commit (e.g. from a change made directly on the host) can never cause a
# "divergent branches" error — this always makes the working copy match
# GitHub exactly, discarding any local commits in the process. Any
# git-tracked file (e.g. data/settings.json, if it's still tracked) gets
# reset to its last-committed content along with everything else.
set -e
cd "$(dirname "$0")"

echo "==> fetching latest"
git fetch origin

echo "==> resetting to origin/main"
git reset --hard origin/main

echo "==> rebuilding and restarting"
docker compose up -d --build

echo "==> done. Recent logs:"
docker compose logs --tail=20
