#!/bin/bash
# Pull the latest code and force Docker to rebuild the image.
#
# Why this exists: Dockge's Deploy/Update button runs the equivalent of
# `docker compose up -d`, which reuses the cached image if one already
# exists — it will NOT rebuild just because the Dockerfile or source
# changed. This script does what the button doesn't.
set -e
cd "$(dirname "$0")"

echo "==> git pull"
git pull

echo "==> rebuilding and restarting"
docker compose up -d --build

echo "==> done. Recent logs:"
docker compose logs --tail=20
