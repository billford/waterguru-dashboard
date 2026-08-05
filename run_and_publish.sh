#!/bin/bash
# Runs a fetch and republishes the dashboard.
#
# The fetch is deliberately NOT under `set -e`: if it dies, that's the one
# failure the Python-side alerting can't report on its own (no data means no
# code path to fire from), so the exit code is caught here and turned into a
# notification. Without this, a broken pipeline looks exactly like a calm pool -
# the dashboard just keeps serving whatever it published last.
set -uo pipefail
cd "$(dirname "$0")"

FETCH_LOG=$(mktemp)
trap 'rm -f "$FETCH_LOG"' EXIT

if ./venv/bin/python fetch.py 2>&1 | tee "$FETCH_LOG"; then
  :
else
  DETAIL=$(tail -n 3 "$FETCH_LOG" | tr '\n' ' ')
  ./venv/bin/python alerts.py fetch-failed "${DETAIL:-fetch.py exited non-zero.}"
  exit 1
fi

npx --yes wrangler pages deploy site --project-name waterguru-dashboard --branch main --commit-dirty=true
