#!/bin/bash
# Runs a fetch and republishes the dashboard.
#
# Neither step is under `set -e`: both failures are ones the Python-side
# alerting can't report on its own, so their exit codes are caught here and
# turned into notifications. Without this, a broken pipeline looks exactly like
# a calm pool - the dashboard just keeps serving whatever it published last.
set -uo pipefail

# Unchecked, a failed cd runs the venv path from the wrong directory: the fetch
# fails to launch AND so does the alert about it, which is silence twice over.
cd "$(dirname "$0")" || { echo "cannot cd to script directory" >&2; exit 1; }

LOG=$(mktemp)
trap 'rm -f "$LOG"' EXIT

# $1 = human label, rest = command
run_step() {
  local label="$1"; shift
  if "$@" 2>&1 | tee "$LOG"; then
    return 0
  fi
  local detail
  detail=$(tail -n 3 "$LOG" | tr '\n' ' ')
  ./venv/bin/python alerts.py step-failed "$label" "${detail:-exited non-zero.}"
  exit 1
}

run_step "Pool data fetch" ./venv/bin/python fetch.py

# The deploy was previously unguarded, so an expired wrangler token or a network
# blip left the site serving the previous build indefinitely, with the failure
# visible only in a log nobody reads.
run_step "Dashboard deploy" npx --yes wrangler pages deploy site \
  --project-name waterguru-dashboard --branch main --commit-dirty=true
