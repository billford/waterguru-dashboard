#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

./venv/bin/python fetch.py
npx --yes wrangler pages deploy site --project-name waterguru-dashboard --branch main --commit-dirty=true
