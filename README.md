# WaterGuru Dashboard

A small pipeline that pulls pool telemetry from a [WaterGuru Sense](https://waterguru.com/)
device, stores it, and publishes it to a little static dashboard with charts and
red/yellow/green alerting.

Live example: https://waterguru-dashboard.pages.dev

## Credit

The hard part — figuring out that WaterGuru's app talks to an AWS Cognito + Lambda
backend with no public API, and reverse-engineering the auth flow (SRP login,
identity-pool credential exchange, signed Lambda invocation) — was done by
[**Brian Wilson**](https://github.com/bdwilson) in
[bdwilson/waterguru-api](https://github.com/bdwilson/waterguru-api). `fetch.py` here
is a from-scratch rewrite of that same auth flow (no Flask/Docker, and using
[`pycognito`](https://github.com/pvizeli/pycognito) instead of the unmaintained
`warrant` library, which doesn't run on modern Python), but the credit for
discovering the API in the first place goes to that project. If you don't own a
WaterGuru, Brian's README has a referral discount link for one.

**Please don't hit the WaterGuru API more than once or twice a day.** There's no
token refresh implemented (same caveat as the original project) — every run is a
fresh login, and the API isn't meant for polling more often than that.

## How it works

```
fetch.py  --auths via Cognito, calls the getDashboardView Lambda--> WaterGuru
   |
   v
db.py     --parses the response into data/waterguru.db (SQLite)
   |
   v
publish.py --exports the last 180 days into site/data/history.json
   |
   v
site/index.html --static dashboard (vanilla JS/SVG, no build step, no CDN deps)
   |
   v
Cloudflare Pages --wrangler pages deploy, no GitHub integration needed
```

`alerts.py` runs after every fetch and fires a notification when a water body's
status transitions to `RED`, or recovers from `RED` back to normal.

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install requests requests_aws4auth boto3 pycognito
cp .env.example .env   # fill in WG_USER / WG_PASS (see below)
./venv/bin/python fetch.py
```

`.env`:

```
WG_USER=your@email.address       # same login as the WaterGuru mobile app
WG_PASS=your_waterguru_password
NTFY_TOPIC=                       # optional, see Alerting below
```

`.env` is gitignored — never commit it. So is `data/waterguru.db` and
`site/data/history.json` (generated data, regenerated on every run).

## Scheduling (twice a day)

`run_and_publish.sh` runs a fetch and then deploys the updated dashboard to
Cloudflare Pages. On macOS, a `launchd` agent runs it at 8am/8pm — see
`com.billfordx.waterguru-fetch.plist` for the schedule (unlike cron, launchd
catches a missed run up on next wake, which matters if the machine sleeps
through the scheduled time).

## Alerting

- **macOS notification** — always on, no setup, via `osascript`.
- **Push via [ntfy.sh](https://ntfy.sh)** — free, no account. Set `NTFY_TOPIC` in
  `.env` to any hard-to-guess string, then subscribe to that topic in the ntfy
  app. Anyone who knows the topic name can read the alerts (ntfy topics aren't
  access-controlled), so don't use something guessable.

## Deploying the dashboard

```bash
npx wrangler login          # one-time browser auth
npx wrangler pages project create waterguru-dashboard --production-branch main
./run_and_publish.sh        # fetch + deploy
```

The dashboard reads `site/data/history.json` client-side — there's no backend,
just a static file that gets overwritten and redeployed on each fetch.
