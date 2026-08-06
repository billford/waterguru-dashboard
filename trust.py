"""Whether a measurement should be believed.

A sensor reading can be precise and still be wrong about the pool. The case that
prompted this: the pump died, water sat stagnant in the skimmer and plumbing for
48 hours, and the pod measured minutes after flow was restored. The numbers it
returned described the water in the pipe, not the water in the pool - calcium
hardness 153 ppm and stabilizer 10 ppm against a hand test that came back
in range.

That reading drove a RED status and, worse, a dose recommendation: 73 cups of
calcium chloride into 20,000 gallons. Chemistry advice derived from an
unrepresentative sample is the most expensive kind of wrong this pipeline can
produce, so measurements carry a trust verdict and untrusted ones are kept out
of anything that models or recommends.

Two sources of verdict:

- **Automatic** - WaterGuru itself reports how long the flow sensor has been
  silent ("No flow sensor report: 48 hours"). A measurement taken at the end of
  a long outage is sampling water that hasn't circulated, so it's flagged.
- **Manual** - `annotate.py`, for everything a rule can't see. If you hand-test
  the water and the sensor disagrees, you're right and it isn't.

Manual verdicts always win; a rule shouldn't overrule someone standing at the
pool with a test kit.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANNOTATIONS_PATH = HERE / "annotations.json"

# Hours of silence from the flow sensor before a following measurement is
# treated as sampling stagnant water. A pool turns over in well under a day of
# normal pump running, so a full day without any flow report means the water at the
# sensor has no particular reason to resemble the rest of the pool.
STAGNANT_HOURS = 24

# Verdicts an annotation may declare. Anything else is not a verdict.
VALID_VERDICTS = {"suspect", "trusted"}

# "2026-08-05T21:10" - enough to identify one measurement. Shorter keys would
# silently apply one verdict to a whole day, month or year.
MIN_ANNOTATION_KEY_LENGTH = len("2026-08-05T21:10")

_FLOW_OUTAGE = re.compile(r"No flow sensor report:\s*(\d+)\s*hours?", re.IGNORECASE)


# ---- annotations ----

def _load_doc(path: Path = None) -> dict:
    path = path or ANNOTATIONS_PATH
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_doc(doc: dict, path: Path = None):
    path = path or ANNOTATIONS_PATH
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def load_annotations(path: Path = None) -> dict:
    return _load_doc(path).get("measurements", {})


def save_annotations(measurements: dict, path: Path = None):
    doc = _load_doc(path)
    doc["measurements"] = measurements
    _save_doc(doc, path)


def load_events(path: Path = None) -> dict:
    """Things done to the pool that the sensor can't see but that explain its numbers."""
    return _load_doc(path).get("events", {})


def save_events(events: dict, path: Path = None):
    doc = _load_doc(path)
    doc["events"] = events
    _save_doc(doc, path)


def water_added_between(start, end, events: dict) -> bool:
    """Did a top-up overlap the period between two measurements?

    Fresh water dilutes everything in it. Chlorine drops, but not because the
    pool consumed any - so the interval says nothing about demand and must not
    be fitted as though it did.

    A fill is a *window*, not an instant: a hose left running for hours dilutes
    throughout. Treating it as a point would keep any interval that began after
    the tap was opened but before it was closed, which is exactly the interval
    most affected. Events with no recorded end are treated as instantaneous,
    which is the safe reading of an unfinished record.
    """
    for stamp, event in (events or {}).items():
        if event.get("type") != "water_added":
            continue
        began = _parse(stamp)
        if began is None:
            continue
        ended = _parse(event.get("ends_at")) or began
        # Any overlap at all between [began, ended] and [start, end].
        if began <= end and ended >= start:
            return True
    return False


def _parse(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def find_annotation(measure_time: str, annotations: dict) -> dict | None:
    """Matches an annotation key against a measurement timestamp by prefix.

    Lets a human type `2026-08-05T21:10` rather than transcribing
    `2026-08-05T21:10:04.000Z` exactly.
    """
    if not measure_time:
        return None
    normalized = str(measure_time).replace("Z", "")
    # Longest match wins, and a key must be specific enough to identify a single
    # measurement. Unbounded prefixes meant the key "2" matched every reading
    # ever taken, and sort order let a vague old key permanently shadow a later,
    # more specific one.
    matches = [
        (key, annotation) for key, annotation in annotations.items()
        if len(str(key)) >= MIN_ANNOTATION_KEY_LENGTH
        and normalized.startswith(str(key).replace("Z", ""))
    ]
    if not matches:
        return None
    return max(matches, key=lambda kv: len(str(kv[0])))[1]


# ---- automatic signals ----

def flow_outage_hours(alerts_json) -> float | None:
    """How long WaterGuru says the flow sensor has been silent, if it says so."""
    try:
        alerts = json.loads(alerts_json) if isinstance(alerts_json, str) else (alerts_json or [])
    except (TypeError, ValueError):
        return None
    for alert in alerts:
        if alert.get("source") != "FLOW_SENSOR":
            continue
        match = _FLOW_OUTAGE.search(alert.get("text") or "")
        if match:
            return float(match.group(1))
    return None


def auto_reasons(preceding_row: dict | None) -> list[str]:
    """Why a measurement taken after `preceding_row` might not be believable."""
    if not preceding_row:
        return []

    outage = flow_outage_hours(preceding_row.get("alerts_json"))
    if outage is not None and outage >= STAGNANT_HOURS:
        return [
            f"Taken after {outage:g}h with no circulation reported - the water at the "
            "sensor may not represent the pool."
        ]
    return []


def evaluate(rows: list[dict], annotations: dict = None) -> dict:
    """Trust verdicts for every distinct measurement in `rows` (oldest first).

    Keyed by measurement timestamp. `rows` are raw snapshots, not deduped: the
    snapshot *preceding* a measurement's first appearance is what carries the
    evidence about conditions leading up to it.
    """
    annotations = annotations if annotations is not None else load_annotations()

    verdicts = {}
    seen = set()
    for i, row in enumerate(rows):
        key = row.get("latest_measure_time") or row.get("fetched_at")
        if key in seen:
            continue
        seen.add(key)

        reasons = auto_reasons(rows[i - 1] if i > 0 else None)
        verdict = {
            "trusted": not reasons,
            "reasons": reasons,
            "source": "auto",
            "note": None,
        }

        manual = find_annotation(key, annotations)
        # Only an explicit, recognised verdict overrides the automatic one.
        # Previously ANY annotation marked the reading trusted and discarded the
        # auto reasons - so `{"note": "topped up"}`, a hand-edit that meant to
        # say nothing about trust, silently un-flagged a stagnant-water reading.
        declared = str((manual or {}).get("verdict") or "").strip().lower()
        if declared in VALID_VERDICTS:
            trusted = declared == "trusted"
            note = manual.get("note")
            reasons = [note] if note else [f"Marked {declared} by hand."]
            # Keep the automatic reason visible when a hand verdict overrides it,
            # so the dashboard can say *what* was overridden.
            if trusted and verdict["reasons"]:
                reasons += [f"(overrides: {r})" for r in verdict["reasons"]]
            verdict = {"trusted": trusted, "reasons": reasons, "source": "manual", "note": note}
        elif manual:
            # An annotation that says nothing about trust still contributes its
            # note, but must not change the verdict.
            verdict = {**verdict, "note": manual.get("note")}
        verdicts[key] = verdict

    return verdicts


def untrusted_keys(rows: list[dict], annotations: dict = None) -> set:
    return {k for k, v in evaluate(rows, annotations).items() if not v["trusted"]}


def filter_trusted(rows: list[dict], untrusted: set) -> list[dict]:
    """Drops measurements that shouldn't inform a model or a recommendation."""
    return [
        r for r in rows
        if (r.get("latest_measure_time") or r.get("fetched_at")) not in untrusted
    ]
