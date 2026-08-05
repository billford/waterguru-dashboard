"""Summarizes recent pool readings with a local LLM via Ollama (localhost:11434) -
no data leaves the machine. Falls back to a rule-based summary if Ollama isn't
reachable, so the dashboard never just breaks.
"""
import json
from pathlib import Path

import requests

import trust
from db import connect, dedupe_by_measurement

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.2:3b"
LOOKBACK_DAYS = 14

# Below this many distinct measurements, the model is not asked at all.
#
# The prompt demands a verdict on whether things are trending up, down, or
# steady, and a small model handed one or two readings will manufacture one to
# comply - observed inventing a chlorine drop from 9.8 to 3.5 ppm and a pH shift
# to 6.9 from a single 9.8/7.3 reading. A trend needs points to draw a line
# through, so with fewer than three the rule-based sentence says plainly that
# there isn't enough history yet.
MIN_READINGS_FOR_LLM = 3


def _recent_rows(water_body_id: str) -> list[dict]:
    """Distinct chemistry measurements from the lookback window, oldest first.

    Deduped on measurement time: fetching twice a day against a cassette that
    measures once a day would otherwise feed the model the same reading twice
    and make a flat stretch look like twice as much corroborating evidence.
    """
    conn = connect()
    rows = conn.execute(
        """SELECT fetched_at, latest_measure_time, status, water_temp, free_cl, ph,
                  skimmer_flow, cassette_pct_left, battery_pct_left, alerts_json
           FROM snapshots
           WHERE water_body_id = ? AND fetched_at >= datetime('now', ?)
           ORDER BY fetched_at""",
        (water_body_id, f"-{LOOKBACK_DAYS} days"),
    ).fetchall()
    conn.close()

    rows = [dict(r) for r in rows]
    # Readings taken from water that hasn't circulated get left out - describing
    # a trend through a bad sample is worse than reporting a shorter history.
    untrusted = trust.untrusted_keys(rows)
    return trust.filter_trusted(dedupe_by_measurement(rows), untrusted)


def _trend_word(first: float, last: float, tolerance: float) -> str:
    delta = last - first
    if abs(delta) <= tolerance:
        return "steady"
    return "rising" if delta > 0 else "falling"


def _rule_based_summary(rows: list[dict]) -> str:
    if not rows:
        return "No readings yet - the first measurement will show up here."
    if len(rows) < MIN_READINGS_FOR_LLM:
        latest = rows[-1]
        current = ", ".join(
            f"{label} {latest[field]}"
            for label, field in (("free chlorine", "free_cl"), ("pH", "ph"), ("water temp", "water_temp"))
            if latest.get(field) is not None
        )
        reading_word = "reading" if len(rows) == 1 else "readings"
        return (
            f"Currently {current}. Only {len(rows)} distinct {reading_word} so far - "
            "not enough history to call a trend yet."
        )

    first, last = rows[0], rows[-1]
    bits = []
    if first["free_cl"] is not None and last["free_cl"] is not None:
        bits.append(f"free chlorine is {_trend_word(first['free_cl'], last['free_cl'], 0.3)} ({last['free_cl']} ppm)")
    if first["ph"] is not None and last["ph"] is not None:
        bits.append(f"pH is {_trend_word(first['ph'], last['ph'], 0.1)} ({last['ph']})")
    if first["water_temp"] is not None and last["water_temp"] is not None:
        bits.append(f"water temp is {_trend_word(first['water_temp'], last['water_temp'], 2)} ({last['water_temp']}°F)")

    reds = sum(1 for r in rows if r["status"] == "RED")
    overall = "solid overall" if reds == 0 else f"flagged red on {reds} of the last {len(rows)} readings"
    return f"Over the last {len(rows)} readings, {', '.join(bits)}. Status looks {overall}."


def _llm_summary(rows: list[dict], name: str) -> str | None:
    lines = [
        f"{(r.get('latest_measure_time') or r['fetched_at'])[:16]}  status={r['status']}  "
        f"free_cl={r['free_cl']}  ph={r['ph']}  temp={r['water_temp']}F  flow={r['skimmer_flow']}"
        for r in rows
    ]
    prompt = (
        f"You are summarizing recent water-quality readings for a residential pool named '{name}' "
        f"for its owner. Each line is one distinct measurement by the sensor, which measures roughly "
        f"once a day - so a short list covers a normal week, not a gap in monitoring. "
        f"Here is the reading history, oldest first:\n\n"
        + "\n".join(lines)
        + "\n\nIn 2-3 short sentences, say whether free chlorine, pH, and water temp are trending up, "
        "down, or holding steady, and whether things look solid or need attention. Be direct and "
        "concrete with numbers. Do not repeat the raw data as a list - write plain prose. No preamble."
    )
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": MODEL, "prompt": prompt, "stream": False},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except requests.RequestException:
        return None


def summarize(water_body_id: str, name: str) -> dict:
    rows = _recent_rows(water_body_id)
    llm_text = _llm_summary(rows, name) if len(rows) >= MIN_READINGS_FOR_LLM else None
    return {
        "text": llm_text or _rule_based_summary(rows),
        "source": "llm" if llm_text else "rule_based",
        "reading_count": len(rows),
    }


def export_summaries(history_path: Path, out_path: Path):
    history = json.loads(history_path.read_text())
    summaries = {}
    for wb_id, wb in history.get("waterbodies", {}).items():
        summaries[wb_id] = summarize(wb_id, wb.get("name", "Pool"))
    out_path.write_text(json.dumps(summaries, indent=2))
    return summaries


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    print(export_summaries(here / "site" / "data" / "history.json", here / "site" / "data" / "summary.json"))
