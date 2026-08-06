# WaterGuru Dashboard

A pipeline that pulls pool telemetry from a [WaterGuru Sense](https://waterguru.com/)
device — an internet-connected chlorine/pH/temp sensor most people only see through
WaterGuru's mobile app — stores it, and publishes it to a small static dashboard.
Twice a day it also asks a **local LLM** for a plain-English trend read and a
day-by-day swim/heater recommendation, using a real weather forecast.

**Live example:** https://waterguru-dashboard.pages.dev

**Repo:** https://github.com/billford/waterguru-dashboard (public)

---

## What it does

- **Pulls pool readings** twice a day (free chlorine, pH, water temp, skimmer flow,
  equipment health) straight from WaterGuru's backend — no official public API,
  see [Credit](#credit) below.
- **Stores history** in SQLite and charts it (chlorine, pH, temp, skimmer flow) with
  target bands, hover tooltips, and a 7/30/90/all date filter.
- **Chlorine outlook** — projects free chlorine forward from this pool's own measured
  burn rate and the temperature forecast, to answer the question the raw number
  doesn't: *do I need to do anything this week?* See
  [Chlorine outlook](#chlorine-outlook) below.
- **Alerts** — a macOS notification and a phone push (via [ntfy.sh](https://ntfy.sh))
  on water chemistry going RED, the cassette or battery needing replacement, a
  reorder reminder with enough lead time to actually order one, a stalled pipeline
  or a silent sensor, and sensor readings that look untrustworthy.
- **Weekly digest** — one Sunday push summarizing the week ahead, so a healthy pool
  isn't indistinguishable from a broken pipeline.
- **Trend summary** — a local LLM reads the last 14 days of readings and writes
  2-3 plain-English sentences on whether things are trending up, down, or steady.
- **5-day swim forecast** — pulled from the National Weather Service, with a local
  LLM judging each day (great/good/marginal/poor) by weighing the forecast against
  the season and the pool's actual current water temp, plus a **heater lead-time
  tip** (the pool is heated, so a cool or warm stretch a few days out is worth
  adjusting for in advance).
- **A swim drill of the day** and a **shark-chase animation** on rough days, because
  a pool dashboard doesn't have to be boring.

All of it runs locally on a Mac via `launchd`, twice a day, and republishes the
static dashboard to Cloudflare Pages after every run. Nothing paid, no backend
server, no database server — just a cron-ish job, a SQLite file, and a static site.

---

## Three clocks

This tripped up the first version of the dashboard and is worth stating plainly,
because it shapes the whole data model: **the device's readings move at three
completely different rates.**

| Reading | Cadence | `cfg.validDays` |
|---|---|---|
| Water temperature | every fetch (minutes) | — |
| Free chlorine, pH, skimmer flow | ~daily | 2 |
| Alkalinity, calcium, stabilizer, hardness | ~monthly | 30 |

Since the fetch runs twice a day, several consecutive snapshots share one
`latest_measure_time` and carry byte-identical chemistry — so charting
everything against `fetched_at` drew a single chlorine measurement as three or
four separate readings, and fed the trend-summary LLM the same reading several
times over, making a flat stretch look like repeated corroboration.

So the export splits them:

- `series` — chemistry, deduped on `latest_measure_time`, timestamped with it.
  Deduping **merges fields rather than taking the last row wholesale**: an
  individual sensor can drop out mid-measurement (the flow sensor going quiet
  nulls `skimmer_flow` while everything else keeps reporting), and taking the
  last row would discard a value the earlier fetches did have.
- `temp_series` — water temperature, one point per fetch, timestamped with
  `fetched_at`.
- The slow panel rides along on the chemistry rows but keeps its own
  `panel_measure_time`, and gets its own dashboard card and its own staleness
  threshold. Judging a 30-day measurement by the 48-hour rule would report a
  perfectly healthy panel as a fault every single day.

The dashboard labels chemistry tiles with their own age for the same reason: a
three-day-old chlorine number sitting next to a live water temp otherwise reads
as equally current.

### Everything the device actually reports

The original pipeline stored three of the seven chemistry values. It now keeps
all of them — total alkalinity, calcium hardness, cyanuric acid (stabilizer) and
total hardness were being parsed straight past and discarded. Stabilizer in
particular turned out to matter well beyond its own tile: it's the main control
on how fast sunlight destroys chlorine, and the forecast now uses it.

Alerts also carry `advice.action.summary` — WaterGuru works out the actual dose
for the pool's volume ("Add 73 cups of 90% concentration calcium chloride"),
which is the genuinely actionable half of an alert and was being dropped. It now
appears in the push and on the dashboard.

---

## Architecture

```mermaid
flowchart TD
    WG[("WaterGuru backend<br/>(AWS Cognito + Lambda,<br/>no public API)")]
    NWS[("National Weather Service<br/>api.weather.gov")]
    OLLAMA{{"Ollama<br/>localhost:11434<br/>(fully local)"}}

    subgraph Mac["This Mac — launchd, twice daily (8am / 8pm)"]
        FETCH["fetch.py<br/>Cognito SRP login → getDashboardView Lambda call<br/>retries transient failures"]
        DB[("data/waterguru.db<br/>SQLite + sent_notifications")]
        PUBLISH["publish.py<br/>last 180 days, chemistry deduped"]
        WEATHER["weather.py<br/>5-day forecast + rule-based swim score"]
        TREND["trend_summary.py<br/>14-day trend read"]
        ADVISOR["swim_advisor.py<br/>per-day verdict + heater advice"]
        CLFC["chlorine_forecast.py<br/>burn rate → when to act"]
        ANOM["anomaly.py<br/>flatline / jump / dropout"]
        ALERTS["alerts.py<br/>chemistry, cassette, battery,<br/>reorder, staleness, anomalies"]
        DIGEST["digest.py<br/>weekly summary push"]
        DEPLOY["run_and_publish.sh<br/>wrangler pages deploy<br/>alerts on fetch failure"]
    end

    subgraph SiteData["site/data/*.json (generated, gitignored)"]
        HIST[history.json]
        SUMM[summary.json]
        WX[weather.json]
        ADV[swim_advice.json]
        CLJ[chlorine_forecast.json]
        ANJ[anomalies.json]
    end

    CF[("Cloudflare Pages<br/>waterguru-dashboard.pages.dev")]
    SITE["site/index.html<br/>vanilla JS/SVG dashboard, no build step"]
    MACOS(["macOS notification"])
    NTFY(["ntfy.sh push"])

    WG -->|"auth + dashboard JSON"| FETCH
    FETCH --> DB
    FETCH --> ALERTS
    DB --> ANOM --> ANJ
    ANOM --> ALERTS
    ALERTS --> MACOS
    ALERTS --> NTFY
    DEPLOY -.->|"on non-zero exit"| ALERTS
    DB --> PUBLISH --> HIST
    NWS -->|"5-day forecast"| WEATHER --> WX
    HIST --> TREND
    OLLAMA <-.->|"prompt / response"| TREND --> SUMM
    WX --> ADVISOR
    HIST --> ADVISOR
    OLLAMA <-.->|"prompt / response"| ADVISOR --> ADV
    DB --> CLFC
    WX --> CLFC --> CLJ
    SUMM --> DIGEST
    CLJ --> DIGEST
    ADV --> DIGEST
    DIGEST -->|"Sundays only"| NTFY
    HIST --> DEPLOY
    SUMM --> DEPLOY
    WX --> DEPLOY
    ADV --> DEPLOY
    CLJ --> DEPLOY
    ANJ --> DEPLOY
    DEPLOY -->|"wrangler pages deploy"| CF
    CF --> SITE
```

Every arrow into `site/data/*.json` happens locally; the only outbound calls per
run are to WaterGuru, the National Weather Service, ntfy.sh (if configured), and
finally Cloudflare when publishing. The LLM calls (dashed arrows) never leave the
machine — Ollama runs on `localhost:11434`.

---

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

`fetch.py` retries a failed fetch up to three times with long backoffs, so a
dropped connection doesn't cost a whole 12-hour slot. Retries are deliberately
scoped: wrong credentials, or any other 4xx, fail immediately rather than
retrying, since three attempts on a bad password is three failed logins against
your account and not a recovery.

---

## Repo layout

```
fetch.py                     # Cognito auth + Lambda call → SQLite → triggers everything below
db.py                        # SQLite schema, row parsing, measurement dedupe, notification state
publish.py                   # SQLite → site/data/history.json
freshness.py                 # data-age math: stalled pipeline vs. silent sensor
consumables.py               # cassette/battery runway + reorder lead times
anomaly.py                   # sensor-health checks → site/data/anomalies.json
trust.py                     # is a reading believable? circulation checks + manual verdicts
annotate.py                  # CLI to mark a measurement suspect or trusted by hand
annotations.json             # hand-recorded verdicts (committed - a record of the pool)
chlorine_forecast.py         # burn-rate fit + projection → site/data/chlorine_forecast.json
weather.py                   # NWS forecast → site/data/weather.json (+ rule-based swim score)
trend_summary.py             # local LLM → site/data/summary.json
swim_advisor.py              # local LLM → site/data/swim_advice.json
alerts.py                    # macOS notification + ntfy.sh push; also the fetch-failure CLI
digest.py                    # weekly summary push
run_and_publish.sh           # fetch.py, then `wrangler pages deploy`; alerts if the fetch dies
com.billfordx.waterguru-fetch.plist   # launchd schedule (8am/8pm)
tests/                       # pytest suite, no network or database access required
site/
  index.html                 # the dashboard — vanilla JS/SVG, no build step, no CDN deps
  about-ai.html              # what's LLM-generated, what's plain arithmetic, and why
  data/                      # generated JSON the dashboard fetches client-side (gitignored)
requirements.txt             # pinned runtime deps
requirements-dev.txt         # the above plus pytest
.env.example                 # template for WG_USER/WG_PASS/NTFY_TOPIC/WX_LAT/WX_LON
```

---

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in WG_USER / WG_PASS (see below)
./venv/bin/python fetch.py
```

Dependencies are pinned — `boto3`/`botocore` especially, since the Cognito SRP
and signed-Lambda flow in `fetch.py` leans on internals that have shifted
between releases before.

### Tests

```bash
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/pytest
```

The suite covers the fiddly parts: measurement deduping, the alert transition
and once-only logic, burn-rate fitting and projection anchoring, the anomaly
thresholds, retry-vs-fail-fast on the fetch, and the WaterGuru payload parsing.
It touches no network and no real database — each test gets a throwaway SQLite
file, and notification channels are stubbed, so running it can't push to your
phone.

`.env`:

```
WG_USER=your@email.address       # same login as the WaterGuru mobile app
WG_PASS=your_waterguru_password
NTFY_TOPIC=                       # optional, see Alerting below
WX_LAT=                           # optional, for the 5-day swim forecast (US only)
WX_LON=
```

`.env` is gitignored — never commit it. So is `data/waterguru.db` and everything
under `site/data/` except `.gitkeep` (all generated, regenerated on every run). The
repo's git history has been audited and contains no real credentials — only the
`.env.example` placeholders were ever committed.

### Local LLM (Ollama)

Both the trend summary and the swim advisor need [Ollama](https://ollama.com)
installed and running locally:

```bash
ollama pull llama3.2:3b     # trend_summary.py — fast, small, plenty for summarizing numbers
ollama pull qwen2.5:32b     # swim_advisor.py — needs more judgment, see below
```

Model choice wasn't arbitrary — for the swim advisor (which has to weigh season,
temperature, and rain into a verdict *and* return strict JSON), three local models
were compared head-to-head:

| Model | Result |
|---|---|
| `llama3.2:3b` | Fast, valid JSON, but inconsistent/illogical verdicts (e.g. flagged a sunny 78°F day as worse than a stormy one) |
| `gpt-oss:20b` | Ignored the JSON output constraint entirely and returned chain-of-thought prose instead |
| `qwen2.5:32b` | **Used.** Reliable JSON every time, sane and consistent verdicts, ~30-45s per run |

Both scripts fall back gracefully if Ollama isn't running or returns something
that doesn't validate: `trend_summary.py` falls back to a rule-based sentence,
`swim_advisor.py` falls back to `weather.py`'s point-based scoring.

### The model isn't asked questions the data can't answer

`trend_summary.py` won't call the LLM at all with fewer than three distinct
measurements. The prompt demands a verdict on whether things are trending up,
down, or steady, and a small model handed one reading will manufacture one to
comply — handed a single `9.8 ppm / 7.3 pH` reading, `llama3.2:3b` reported a
chlorine drop to "approximately 3.5" and a pH shift to 6.9. Neither happened.

This was invisible until chemistry deduping landed: seven duplicate rows of one
measurement had accidentally read as a legitimate "holding steady". Below the
threshold the dashboard now reports the current numbers and states plainly that
there isn't enough history to call a trend — which is the honest answer, and the
one a pool owner can actually act on.

---

## Scheduling (twice a day)

`run_and_publish.sh` runs a fetch (which triggers alerts, weather, trend summary,
and the swim advisor) and then deploys the updated dashboard to Cloudflare Pages.
On macOS, a `launchd` agent runs it at 8am and 8pm — see
`com.billfordx.waterguru-fetch.plist`. `launchd` (vs. cron) catches a missed run
up on next wake, which matters if the machine was asleep at the scheduled time.

```bash
launchctl load ~/Library/LaunchAgents/com.billfordx.waterguru-fetch.plist
```

---

## Alerting

- **macOS notification** — always on, no setup, via `osascript`.
- **Push via [ntfy.sh](https://ntfy.sh)** — free, no account. Set `NTFY_TOPIC` in
  `.env` to any hard-to-guess string, then subscribe to that topic in the ntfy
  app. Anyone who knows the topic name can read the alerts (ntfy topics aren't
  access-controlled), so don't use something guessable.

Alerts come in two shapes, and the difference is what keeps them from becoming
noise you learn to ignore.

**Transitions** fire on the edge and go quiet while the condition persists,
comparing against the previous snapshot. No stored state needed.

- **Water chemistry status** — RED, and again when it recovers. Never `YELLOW`.
- **Cassette replacement** — WaterGuru reports its own `status`/`urgent` flag on
  the cassette (the consumable sensing pad). When it flips to `RED`/urgent you
  get a "replace the cassette" alert with the current %/days-left, and a second
  once it's back to `GREEN`.
- **Pod battery** — the same treatment. The battery level was already being
  stored but nothing ever watched it.

**Standing conditions** have no edge to hang off — left alone they'd re-fire
every run forever — so they're recorded in a `sent_notifications` table and
suppressed until the situation actually resets.

- **Reorder reminders** — `days_left` turned into "order one now" while there's
  still time for it to arrive. Fires once per depletion cycle: putting in a new
  cassette makes its level jump, which clears the record and re-arms the
  reminder for next time, rather than it being a one-time-ever event.
- **Stalled pipeline** — if `fetch.py` exits non-zero, `run_and_publish.sh`
  catches it and pushes. This is the one alert that can't come from the data,
  because the failure *is* the absence of data. Without it a broken pipeline
  looks exactly like a calm pool: the dashboard just keeps serving whatever it
  published last.
- **Silent sensor** — the sneakier version: fetches succeed, WaterGuru answers,
  but `latest_measure_time` stops advancing. A dead cassette, a flat battery, or
  a pod knocked out of the skimmer. **Suppressed during setup**: after
  installation a Sense spends 36 hours (72 scans × 30 min) watching for flow to
  learn when the pump runs, before it will schedule daily measurements. Taking
  no readings in that window is correct behaviour, and alerting through it cries
  wolf on every new install — at exactly the moment an owner is least able to
  tell a real fault from normal setup. The guard checks both the device's own
  `pumpScanState` and the time since `setUpTime`, since either alone can
  mislead.
- **Sensor anomalies** — see below.

The softer standing conditions re-arm weekly, so a real problem resurfaces
instead of being permanently muted by one dismissed notification.

---

## Chlorine outlook

The dashboard could always tell you chlorine was 9.8 ppm against a 3.0 target.
What it couldn't tell you is the thing you act on: whether to reach for the
chlorine this week, or wait.

`chlorine_forecast.py` fits a burn rate from the pool's own history. Between
consecutive measurements chlorine only falls on its own, so any *increase* means
someone added some — those stretches are discarded rather than averaged in. The
remaining declines give a ppm/day rate, and the median is taken so one bad
reading can't drag the model. Each rate is normalized to a reference 80°F before
pooling and re-scaled per forecast day against the NWS temperatures, because
chlorine burns faster in heat and sun.

Two details that matter more than they sound:

- **The projection starts today, not at the last measurement.** The cassette
  measures roughly daily, so the newest reading is usually already a day or two
  old. Decay is walked forward from the measurement so the arithmetic stays
  continuous, then trimmed to today onward — projecting into days that have
  already happened isn't a forecast. When the reading is stale the headline
  leads with the *estimate* ("last measured 9.8 ppm 2 days ago; estimated 5.6
  ppm now"), because the number on the sensor isn't the number in the pool.
- **With too little history it says so.** Under three usable declines it falls
  back to a generic outdoor-pool loss rate and the card marks itself
  low-confidence, rather than presenting a guess in the same voice as a
  measurement.

**The model runs on water temperature, not air.** An earlier version normalized
the fit by water temperature and re-applied it using the NWS air forecast, which
does not cancel — it overstated a genuine 2.0 ppm/day burn as 3.2. Water
temperature is now used at both ends and held at its last measured value across
the projection, which is reasonable for a heated pool on a setpoint.

**Stabilizer is modelled too.** Cyanuric acid is the single biggest control on
how fast sunlight destroys chlorine — an unstabilized pool in summer sun can
lose most of its free chlorine in a day, while 30–50 ppm slows that dramatically
— and the device reports it on the monthly panel. Both adjustments are applied
the same careful way: segments are *divided* by their temperature and CYA
factors before being pooled, then the baseline is *multiplied* by the projected
day's factors. That normalization is what stops the model double-counting, since
a rate fitted from a hot, unstabilized week already has that burn baked in. It
also means the projection responds correctly when conditions change: add
stabilizer, and the same fitted history projects a slower burn.

When stabilizer is low, the card says so directly, because the intuitive
response to chlorine vanishing is to add more chlorine — which treats the
symptom while sunlight keeps destroying it as fast as it goes in.

**"In range" means what the device means.** WaterGuru ships the band it actually
judges chlorine against (green 1.6–5.4 ppm around a 3.0 target), so that's what
gets stored and used. An earlier version compared against target ±35%, which
called a perfectly green 5.2 ppm reading "high".

Both models are approximations — rate-per-10-degrees for heat, coarse bands for
stabilizer. They don't model UV index or bather load. It's a trend projection,
not a chemistry calculator, and the dashboard says as much.

---

## Whether a reading should be believed

A sensor reading can be precise and still be wrong about the pool.

The case this exists for: the pump failed, water sat stagnant in the skimmer and
plumbing for 48 hours, and the pod measured minutes after flow was restored. It
reported calcium hardness 153 ppm and stabilizer 10 ppm — a hand test with a kit
came back in range across the board. The device was measuring the water in the
pipe, not the water in the pool.

That single reading drove a RED status, the chlorine burn-rate fit, the trend
summary, and — worst — a dose recommendation of **73 cups of calcium chloride
into 20,000 gallons**. Chemistry advice derived from an unrepresentative sample
is the most expensive thing this pipeline can produce, so measurements now carry
a trust verdict, and untrusted ones are kept out of anything that models or
recommends.

**Automatic.** WaterGuru reports how long the flow sensor has been silent ("No
flow sensor report: 48 hours"). A measurement landing at the end of an outage
longer than `STAGNANT_HOURS` is sampling water that hasn't circulated, so it's
flagged. The signal comes from the device's own alert rather than being inferred
from gaps in our data.

**Manual**, via `annotate.py` — for everything a rule can't see:

```bash
python annotate.py list
python annotate.py suspect 2026-08-05T21:10 --note "pump was dead, hand test in range"
python annotate.py trust 2026-08-06T20:56
python annotate.py clear 2026-08-05T21:10
```

Timestamps match by prefix, so `2026-08-05T21:10` finds
`2026-08-05T21:10:04.000Z`. Verdicts live in `annotations.json` (committed — it's
a record of what happened to the pool, not generated output). **A hand verdict
always beats the automatic one:** someone standing at the pool with a test kit
outranks a heuristic.

The same file records **events** — things done to the pool that the sensor can't
see but which explain its numbers:

```bash
python annotate.py water-added --note "topped up after the level got low"
```

Topping up dilutes everything in the pool, so chlorine falls without any of it
being consumed. Fitted as decay, a large top-up looks like a catastrophic burn
rate and the forecast predicts the pool stripping itself bare within a day.
Intervals spanning a recorded top-up are excluded from the fit, the same way
intervals where chlorine *rose* are — both describe something done to the pool
rather than something the pool did.

Dilution also drags stabilizer down, which raises the real burn rate, so a
top-up genuinely does change the chemistry — just not by the amount a naive fit
would infer.

An untrusted measurement is dropped from the burn-rate fit and the trend
summary, banner-flagged on the dashboard with its affected tiles dimmed, and any
dose recommendation derived from it gets a "confirm with a test kit before
adding anything" warning attached to the push. If *every* recent reading is
untrusted, the forecast reports that it has nothing dependable to project from
rather than projecting anyway.

---

## The pool controller (Pentair IntelliCenter)

The WaterGuru sensor reports what's *in* the water. The Pentair IntelliCenter
that runs the pump, gas heater, salt cell, lights and water features reports
what the system is *doing* — which is often the explanation for what the sensor
sees. It answers on port 6680 over a local WebSocket with no authentication, so
`pentair.py` reads it directly. **It is strictly read-only**: the same API would
happily start the heater, and this code never writes.

Set `PENTAIR_HOST` in `.env`. To find it: `nmap -p 6680 192.168.1.0/24`, or the
controller advertises itself over mDNS.

Protocol notes, since it's undocumented and the shape isn't obvious:

- `GetQuery/GetHardwareDefinition` returns the object tree, but nested children
  live under `CIRCUITS`, not the `OBJLIST` the top level suggests.
- `GetQuery/GetConfiguration` is what actually lists bodies, circuits, features.
- **`GetParamList` answers under `objectList`; `GetQuery` answers under
  `answer`.** Read the wrong key and a perfectly good `200` looks empty.
- `objnam: "ALL"` and `condition: "OBJTYP=..."` aren't supported on IC 2.019 —
  objects must be named explicitly, so they're discovered from the config first.

### What it changed

- **This pool has a salt cell, running at 60%.** Chlorine isn't only decaying
  here, it's being *generated* whenever the pump runs. The chlorine forecast's
  "assumes no chlorine is added" is wrong for this pool — see the caveat there.
- **The two systems disagree about pool volume**: the controller says 15,000
  gallons, WaterGuru is configured for 20,000. WaterGuru sizes every dose
  recommendation from its own figure, so if the controller is right, doses like
  "add 73 cups of calcium chloride" are ~33% too high. The dashboard flags the
  disagreement rather than picking a winner — the controller's number is only
  whatever was entered at installation.
- **Salt at 4,350 ppm** is above the ~3,000–3,500 these cells want. Too high
  doesn't sanitize better; it corrodes fittings and can fault the cell.
- **A cell at 60% while stabilizer sits near 10 ppm** is the whole story in one
  line: chlorine is being generated hard and destroyed by sunlight almost as
  fast. Raising CYA does more than raising output.

---

## Volume-corrected doses

WaterGuru computes every dose recommendation from the pool volume configured in
its app. If that figure is wrong, so is every dose — and these are instructions
like "add 73.3 cups of calcium chloride", where being a third out is a real
overshoot that has to be diluted back out over weeks.

The controller's volume is cross-checked against WaterGuru's on every run.
When they disagree by more than 10%, `dosing.py` shows the corrected figure
beside the original, on the dashboard and in the push. **It never rewrites
WaterGuru's text** — silently editing someone else's dosing advice is its own
kind of dangerous, so the original stays visible and the correction is additive.

Getting the volume right in the WaterGuru app is the real fix, after which this
does nothing. It stays as a tripwire in case the two ever diverge again.

---

## Measuring pump runtime

A salt cell only makes chlorine while water is moving, so an output percentage
is meaningless without the runtime it was applied over — and two samples a day
cannot tell an eight-hour schedule from a twenty-four-hour one.

The controller is on the LAN, unauthenticated and free to query, so unlike the
WaterGuru API there's no reason to be sparing. `poll_system.py` samples it every
ten minutes via `com.billfordx.pool-poll.plist`, writing to the same
`system_snapshots` table the twice-daily run uses. It never touches the
WaterGuru API and never publishes.

```bash
launchctl load ~/Library/LaunchAgents/com.billfordx.pool-poll.plist
```

---

## The salt cell

The forecast originally modelled chlorine as decaying and nothing else, which is
wrong for a salt pool: the cell manufactures chlorine continuously whenever
water moves.

**The decomposition that matters.** What the sensor observes between two
measurements is the *net* of two opposing processes:

```
observed change = generation − gross loss
```

A fitted decline is therefore not the pool's chlorine demand — it's demand minus
whatever the cell was making at the time. Projecting that net forward is fine as
long as nothing changes, which is why the decay-only model was never visibly
wrong. It breaks the moment you ask *"what if I turn the cell down?"*, because
that needs the terms separated:

```
gross loss        = observed net decline + generation at the settings in force
net at new output = generation(new %) − gross loss
```

**The cell.** A Pentair IntelliChlor Plus40 (part 523735) makes 1.40 lb of
chlorine per 24h at 100%. That's a mass, so converting to ppm needs the volume —
and this pool is 15,000 gallons against a cell rated for 40,000, which is why it
runs hot at modest settings. At 100% with the pump running continuously it would
add **11.2 ppm/day**; at the current 50%, **5.6 ppm/day**.

**Runtime is half the answer.** The cell only produces while the pump runs, so
8h versus 24h is a threefold difference in the result. `poll_system.py` measures
it; `PUMP_RUNTIME_HOURS` in `.env` declares it when the schedule is simply known
(this pump runs 23:45 with a 15-minute cool-down, so 23.75). Measured beats
declared, but waiting days to rediscover a schedule you can read off the
controller is silly.

**Why a rise isn't discarded.** The fit originally threw away any interval where
chlorine went *up*, on the grounds that it meant someone dosed the pool. In a
salt pool a rise usually just means the cell outproduced the loss — and with an
oversized cell most intervals rise, so that rule discarded nearly everything and
left the model unable to ever fit a rate. Demand is now recovered from either
direction:

```
demand = generation − observed net change
```

An interval is only discarded when chlorine rose *faster than the cell could
possibly have raised it*, which does mean it was dosed by hand. With
`generation = 0` this reduces exactly to the old behaviour.

This also explains why the decay-only model was never visibly wrong: a rate
fitted from observed data already contains the cell's contribution, so adding
generation to the projection and adding it back into demand cancel out. The net
is preserved. Separating the terms changes nothing about *this* projection — it's
what makes "what if I change the output?" answerable at all.

**No recommendation until both inputs are measured.** The output percentage that
would hold chlorine steady is only offered once the pool's own demand has been
fitted *and* pump runtime measured. Derived from a generic loss rate and an
assumed schedule it would be a guess compounded with a guess — and in testing it
confidently advised turning the cell *up* while chlorine sat above the top of
range. Until then the card shows what the cell contributes and says plainly
what's missing.

---

## What day is it at the pool?

Timestamps are stored in UTC and that was never the problem. The problem was
**day-boundary decisions** made in UTC when everything they're compared against
is local: NWS forecasts are keyed by local calendar days, the pod's measurement
schedule is local, and "today" means the owner's today.

`launchd` runs at 08:00 and 20:00 local. In an eastern summer that evening run
is **00:00 UTC the next day**, so half of all runs believed it was tomorrow:

- the chlorine projection skipped the current day and priced tomorrow as today,
  discarding the forecast temperature for the day being lived in;
- the swim advisor's prompt asserted the wrong weekday, so every verdict and the
  heater advice reasoned about the wrong day;
- the weekly digest fired **Saturday evening**.

`poolclock.py` centralises this. The clock comes from the machine running the
pipeline, which is on the controller's LAN and therefore at the pool — and
crucially is DST-aware. The controller reports a *fixed* `TIMZON` (−5 here) with
no daylight-saving information, so trusting it would put the pool an hour out
for most of the swimming season; it's used as a **cross-check** instead, and a
disagreement larger than DST can explain gets reported. `POOL_TZ_OFFSET`
overrides for running the pipeline away from the pool.

The digest also stopped asking "is it Sunday?" and now asks "when did one last
go out?" — a Mac asleep through both Sunday windows used to drop that week
entirely, with no record and no retry. It's the pipeline's only heartbeat, so a
silently skipped one is the worst failure it has; a missed Sunday is now caught
up on the next run.

---

## What the pump telemetry gives us

The variable-speed pump reports live **RPM, flow (GPM) and power (watts)** — the
only real-time equipment telemetry the controller offers, and it appears in no
configuration query, so it has to be probed by name. See `PENTAIR_API.md`.

### Detecting a restriction before anything else notices

A variable-speed pump holds the speed it's told to hold. So at a *fixed* RPM,
the flow it achieves measures how hard the water finds it to get through: a
loading filter, a clogging skimmer basket, a closing valve — or a cassette
wedging a skimmer weir open.

That last one actually happened here, and took two days to find. It surfaced
only when WaterGuru's flow sensor went silent and the pod stopped measuring
entirely, by which point the chemistry was two days stale and a panel reading
had been taken from water that hadn't circulated. **Flow at fixed RPM would have
been visibly falling throughout** — and the pump console, which reports total
system flow, showed a perfectly healthy 51 gpm the whole time, because the pump
was fine. It was one skimmer that was starved.

Comparisons are made within an RPM bucket, since comparing raw flow across
speeds would read every schedule change as a fault, and both sides use medians
because a single low sample means nothing.

### Detecting additions nobody logged

The equipment log records what the controller did. The event log records what
you said you did. `interventions.py` covers the gap between them: chemistry that
moved in a direction the pool **cannot move on its own**, which means a person
did it.

That gap matters for a pool under service. A visit that adds fifty pounds of
salt and a jug of chlorine leaves no note, no receipt in the controller, and no
entry anywhere — but it does leave a signature in the numbers.

- **Salt** is conservative: it falls only through dilution and rises only
  through addition. Evaporation concentrates it too, but slowly, so a *step*
  over hours is a bag of salt while a drift over days isn't. A 320 ppm step on
  15,000 gallons is one 40 lb bag, and the detector says so.
- **Chlorine** is bounded by what the cell could have produced. The comparison
  uses the cell at **full** output with nothing lost — deliberately generous, so
  only a rise it could not possibly account for is reported.

It reports what the numbers show and converts to familiar units. It does not
attribute motive or judge whether an addition was warranted: a visit adding salt
to a pool that needed salt looks identical to one adding salt to a pool that
didn't, and only the reading it responded to tells you which. That's a judgement
for the person reading the dashboard, not the dashboard.

### Inferring top-ups from salt

Salt is conservative — it doesn't evaporate, degrade in sunlight, or get
consumed. The cell recycles it. So the only ordinary way salt concentration
*falls* is dilution, which makes the salt reading an accidental flowmeter for
top-ups: if salt goes from S₀ to S₁, the fraction of the pool that's fresh water
is `1 − S₁/S₀`.

This matters because dilution corrupts the chlorine burn-rate fit, and top-ups
were previously recorded by hand — which works right up until you forget, and
forgetting is the normal case, because the notebook is never where the pool is.
Detected top-ups are recorded as ordinary events but tagged as inferred, so
they're distinguishable from something you actually observed. Evaporation moves
salt the other way and is never read as a top-up.

### Heater advice that names a number

The swim advisor now gets the actual setpoint, whether the heater is enabled,
and whether it's currently firing. Before this it could say "consider adjusting
the setpoint" but never "raise it from 81 to 84 on Thursday" — it had never seen
the setpoint.

---

## Log rotation, and why it isn't the usual kind

The poller runs 144 times a day, so anything it prints accumulates forever. But
rotating these logs has a constraint that isn't obvious:

**launchd opens `StandardOutPath`/`StandardErrorPath` itself and holds the
descriptor for the life of the process.** Rename or unlink that file and launchd
carries on writing to the now-unlinked inode — the visible log sits empty, the
disk fills anyway, and nothing indicates why. It's the classic
logrotate-without-copytruncate failure, and on a job running every ten minutes
it would take a long time to notice.

So rotation is split by who owns the descriptor:

- **Python-owned** (`poll.log`) — the process writes it, so ordinary
  `RotatingFileHandler` renaming is safe. The plist points launchd's stdout at
  `/dev/null` so it never opens the file at all.
- **launchd-owned** (the `*.err.log` streams, which must stay with launchd to
  capture failures before Python starts — a missing interpreter, say) — these
  are truncated **in place**, preserving the inode, so launchd's `O_APPEND`
  descriptor keeps writing to the visible file. The tail is kept and a partial
  first line dropped.

Trimming runs at the start of each fetch and each poll, and a failure in it is
swallowed: log housekeeping must never be the thing that breaks a run.

---

## Sensor health

WaterGuru's status flags answer "is the water OK?". They don't answer "is the
*sensor* OK?", and a pod that fails quietly is worse than one that fails loudly
— the dashboard stays green and you stop looking at it.

`anomaly.py` runs three checks over deduped measurements:

- **Flatline** — the same value to the decimal across five distinct
  measurements. Real pool chemistry drifts; identical readings suggest a spent
  cassette pad or a cached value, not a miraculously stable pool.
- **Implausible jump** — a change larger than the chemistry can move in the
  elapsed time. The bound scales with the gap between readings, and is looser
  for chlorine than pH, since shocking a pool really does swing chlorine hard
  while pH is buffered by total alkalinity and moves slowly.
- **Dropout** — a channel that used to report and has gone null for three
  measurements running, which is how a partially failed cassette looks.

---

## Weekly digest

Alerts only fire on problems, which means a healthy pool is silent — and a
silent pipeline is indistinguishable from a broken one until you go looking.

`digest.py` sends one push on Sunday mornings: the trend summary, the chlorine
outlook, the best swim days ahead, and how much cassette and battery runway is
left. It assembles from the JSON the rest of the run already wrote, so it
reflects exactly what the dashboard shows, and it's keyed by ISO week — a
catch-up run after a sleeping Mac won't send a second copy.

It needs `NTFY_TOPIC` set to reach your phone; without it the digest still fires
as a local macOS notification. To preview one without waiting for Sunday:

```bash
./venv/bin/python digest.py --force
```

---

## Deploying the dashboard

```bash
npx wrangler login          # one-time browser auth
npx wrangler pages project create waterguru-dashboard --production-branch main
./run_and_publish.sh        # fetch + deploy
```

The dashboard reads `site/data/*.json` client-side — there's no backend, just
static files that get overwritten and redeployed on each fetch. Deployment is a
direct `wrangler pages deploy` (no GitHub↔Cloudflare app integration), which is
why the GitHub repo can be public while the deploy step still just needs a
Cloudflare account and API auth, nothing shared with GitHub.

**Note on privacy:** the `pages.dev` URL is unlisted but not access-controlled —
anyone with the link can see pool status and history. That's an accepted
trade-off here; Cloudflare Access can gate it behind a login if that changes.
