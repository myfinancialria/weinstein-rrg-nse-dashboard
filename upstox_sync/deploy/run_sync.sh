#!/usr/bin/env bash
# Daily Upstox sync on the Oracle VM: fetch options + FII/DII (+ fundamentals on
# Saturdays or when stale), then commit dashboard/upstox_data.json and push so the
# next GitHub Actions build reads fresh ratios and Pages serves the new data.
#
# Schedule this ~20:00 IST, BEFORE the Actions build at 21:00 IST (see
# deploy/crontab.example). Pushing dashboard/*.json does NOT self-trigger the
# workflow (pages.yml path-ignores it) — the scheduled run publishes it.
#
# Requires: a populated .env with a valid UPSTOX_TOKEN (refresh daily — Upstox has
# no headless login), and git push access to this repo.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

[ -f .env ] && { set -a; . ./.env; set +a; }
PYTHON="${PYTHON:-python3}"

# Fundamentals are quarterly data — force a full refresh on Saturdays; other days
# reuse the committed block unless it is older than --max-age-days (7).
FLAGS=""
if [ "$(date +%u)" = "6" ]; then
  FLAGS="--fundamentals"
  echo "[$(date '+%F %T %Z')] Saturday — forcing fundamentals refresh"
fi

echo "[$(date '+%F %T %Z')] upstox sync $FLAGS"
if ! "$PYTHON" -m upstox_sync sync $FLAGS; then
  echo "[$(date '+%F %T %Z')] sync failed (token expired? re-run login)" >&2
  exit 1
fi

git add dashboard/upstox_data.json
if git diff --cached --quiet; then
  echo "no data change to commit"
else
  git commit -q -m "data: upstox sync $(date '+%F %T %Z')"
  git push -q origin HEAD:main && echo "pushed"
fi
