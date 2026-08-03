"""Summarizes recent pool readings with a local LLM via Ollama (localhost:11434) -
no data leaves the machine. Falls back to a rule-based summary if Ollama isn't
reachable, so the dashboard never just breaks.
"""
import json
import sqlite3
from pathlib import Path

import requests

from db import DB_PATH

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.2:3b"
LOOKBACK_DAYS = 14


def _recent_rows(water_body_id: str) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT fetched_at, status, water_temp, free_cl, ph, skimmer_flow,
                  cassette_pct_left, battery_pct_left
           FROM snapshots
           WHERE water_body_id = ? AND fetched_at >= datetime('now', ?)
           ORDER BY fetched_at""",
        (water_body_id, f"-{LOOKBACK_DAYS} days"),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _trend_word(first: float, last: float, tolerance: float) -> str:
    delta = last - first
    if abs(delta) <= tolerance:
        return "steady"
    return "rising" if delta > 0 else "falling"


def _rule_based_summary(rows: list[dict]) -> str:
    if len(rows) < 2:
        return "Not enough history yet to call a trend - check back after a few more readings."

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
        f"{r['fetched_at'][:16]}  status={r['status']}  free_cl={r['free_cl']}  ph={r['ph']}  "
        f"temp={r['water_temp']}F  flow={r['skimmer_flow']}"
        for r in rows
    ]
    prompt = (
        f"You are summarizing recent water-quality readings for a residential pool named '{name}' "
        f"for its owner. Here is the reading history, oldest first:\n\n"
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
    llm_text = _llm_summary(rows, name) if rows else None
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
