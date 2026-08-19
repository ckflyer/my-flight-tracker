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

# `reset --hard` below rewrites every TRACKED file. Untracked ones (your
# database, settings, session key) are left alone — that is the intent. But
# .gitignore does not untrack a file that was already committed once, so if
# any of these were added to the repo before being ignored, they are being
# silently overwritten on every single update. That looks like settings
# reverting on their own, or everyone being logged out after a deploy.
TRACKED_DATA=$(git ls-files data/ | grep -v '^data/\.gitkeep$' || true)
if [ -n "$TRACKED_DATA" ]; then
  echo ""
  echo "  WARNING: these files under data/ are tracked by git and are about"
  echo "  to be overwritten with whatever is committed in the repo:"
  echo "$TRACKED_DATA" | sed 's/^/      /'
  echo ""
  echo "  To stop that, run once (from this directory):"
  echo "$TRACKED_DATA" | sed 's/^/      git rm --cached /'
  echo "      git commit -m 'stop tracking runtime data' && git push"
  echo ""

  # THE SECRET KEY IS A DIFFERENT PROBLEM FROM THE REST (1.23.1).
  #
  # For a database or a settings file, untracking is the whole fix: the
  # damage was that the deploy kept overwriting them. For the session key
  # it is only half. That key SIGNS EVERY COOKIE, so anyone holding it can
  # forge a session — and `git rm --cached` removes it from the next
  # commit, not from the history that already contains it. Anyone who can
  # read the repo, now or from any clone taken since it was committed,
  # has a working key until it is changed.
  #
  # Said separately and loudly because the generic advice above reads as
  # if it finishes the job, and here it does not.
  if echo "$TRACKED_DATA" | grep -q 'secret_key'; then
    echo "  !! data/secret_key.txt SIGNS EVERY LOGIN COOKIE, and it is in your"
    echo "     git history. Untracking it does NOT remove it from history."
    echo "     Anyone who can read the repo can forge a session until you"
    echo "     change the key. To change it, add a new one to"
    echo "     docker-compose.yml under the app service:"
    echo ""
    echo "         environment:"
    echo "           - PT_SECRET_KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    echo ""
    echo "     then: docker compose up -d"
    echo "     Everyone is signed out once. Log in again; viewers re-enter"
    echo "     their share code. Nothing else is lost."
    echo ""
  fi
fi

echo "==> resetting to origin/main"
git reset --hard origin/main

echo "==> rebuilding and restarting"
docker compose up -d --build

echo "==> done. Recent logs:"
docker compose logs --tail=20
