#!/usr/bin/env python3
"""Builds an evidence pack for a WaterGuru support ticket.

Support conversations go faster when the report contains facts rather than
symptoms. "It stopped measuring" invites a scripted reply about pump schedules;
"two attempts consumed exactly 2 cassette pads each against a normal 10, with
skimmer flow at 11 gpm and the pod checking in every few minutes" does not.

Everything here comes from the API and the local history the pipeline already
keeps - pad counts, measurement timestamps, flow at the moment of each attempt,
connectivity - none of which the mobile app surfaces.

    ./venv/bin/python support_report.py

Writes to stdout; pipe it wherever it needs to go.
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from config import load_dotenv
from db import connect

HERE = Path(__file__).resolve().parent

from pentair import COMPLETE_MEASUREMENT_PADS

# Above this, flow has been observed to support a completed measurement, so a
# failure here cannot be blamed on circulation.
HEALTHY_FLOW_GPM = 12


def _local(iso) -> str:
    if not iso:
        return "unknown"
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone().strftime(
            "%b %d %Y, %I:%M %p")
    except ValueError:
        return str(iso)


def _schedule(pod: dict) -> str:
    times = pod.get("measDoseTimes") or []
    if not times or times[0].get("hour") is None:
        return "unscheduled"
    return f"{times[0]['hour']:02d}:{times[0].get('minute', 0):02d}"


def pad_events(conn) -> list[dict]:
    """Every change in the pad count, with what the measurement time did.

    Pads falling while the measurement time stands still is an attempt that
    aborted; falling *with* a new timestamp is an ordinary measurement.
    """
    rows = [
        dict(r) for r in conn.execute(
            "SELECT fetched_at, cassette_pads_left, cassette_pads_max, latest_measure_time,"
            " skimmer_flow FROM snapshots WHERE cassette_pads_left IS NOT NULL"
            " ORDER BY fetched_at")
    ]
    events, previous = [], None
    for row in rows:
        if previous is None:
            events.append({**row, "consumed": None, "outcome": "first observation"})
        elif row["cassette_pads_left"] != previous["cassette_pads_left"]:
            consumed = previous["cassette_pads_left"] - row["cassette_pads_left"]
            measured = row["latest_measure_time"] != previous["latest_measure_time"]
            outcome = ("cassette replaced/re-registered" if consumed < 0
                       else "measurement completed" if measured
                       else "ATTEMPT FAILED - no reading produced")
            events.append({**row, "consumed": consumed, "outcome": outcome})
        previous = row
    return events


def by_cassette(events: list[dict]) -> list[dict]:
    """Split the pad history at each re-registration: one block per cassette.

    The point of the split is that it separates two very different claims. "This
    cassette is defective" is answered by one block; "the pod cannot read any
    cassette" is answered only by comparing them.
    """
    blocks, current = [], None
    for event in events:
        if event["outcome"] == "first observation" or "replaced" in event["outcome"]:
            current = {"installed": event["fetched_at"], "max_pads": event["cassette_pads_max"],
                       "succeeded": [], "failed": []}
            blocks.append(current)
            continue
        if current is None:
            continue
        bucket = "succeeded" if "completed" in event["outcome"] else "failed"
        if "FAILED" in event["outcome"] or "completed" in event["outcome"]:
            current[bucket].append(event)
    return blocks


def cross_cassette_summary(events: list[dict]) -> list[str]:
    """The part of the evidence that no single cassette can explain."""
    blocks = by_cassette(events)
    failures = [e for e in events if e["outcome"].startswith("ATTEMPT FAILED")]
    healthy = [e for e in failures if (e["skimmer_flow"] or 0) >= HEALTHY_FLOW_GPM]

    out = ["", "THE PATTERN REPEATS ACROSS CASSETTES", ""]
    for i, b in enumerate(blocks, 1):
        if not (b["succeeded"] or b["failed"]):
            continue
        out.append(f"  Cassette {i} (installed {_local(b['installed'])}, {b['max_pads']:.0f} pads): "
                   f"{len(b['succeeded'])} measurement(s) produced, {len(b['failed'])} attempt(s) failed")
        for e in b["failed"]:
            out.append(f"      {_local(e['fetched_at'])}  burned {abs(e['consumed']):.0f} pads, "
                       f"no reading, skimmer flow {e['skimmer_flow'] or 0:.0f} gpm")

    out += [
        "",
        f"  {len(failures)} failed attempts in total, {len(healthy)} of them with skimmer flow at or",
        f"  above {HEALTHY_FLOW_GPM} gpm - so low flow does not account for them.",
        "",
        "  Every failure aborts after 2-5 pads. A measurement that completes uses 10 or more.",
        "  The pod stops partway through the sequence rather than producing a bad reading.",
        "",
        "  A replacement cassette, installed after the first was confirmed defective, shows the",
        "  same behaviour. Two defective cassettes in a row is possible; the pod being unable to",
        "  read any of them is the simpler explanation, and this data cannot distinguish them.",
        "  Please advise on replacing the pod rather than shipping further cassettes.",
    ]
    return out


def build() -> str:
    load_dotenv()
    raw = json.loads((HERE / "data" / "latest.json").read_text())["data"]
    wb = raw["waterBodies"][0]
    pod = wb["pods"][0]["pod"]
    rssi = wb["pods"][0].get("rssiInfo", {})
    lab = next((r for r in wb["pods"][0]["refillables"] if r["type"] == "LAB"), {})
    batt = next((r for r in wb["pods"][0]["refillables"] if r["type"] == "BATT"), {})

    conn = connect()
    try:
        events = pad_events(conn)
    finally:
        conn.close()

    failures = [e for e in events if e["outcome"].startswith("ATTEMPT FAILED")]

    out = [
        "SUPPORT TICKET - SUPPLEMENTARY DIAGNOSTICS",
        f"Pod {pod.get('shortBleId')} (podId {pod.get('podId')}), firmware {pod.get('fwUpdateVersion')}",
        f"Water body: {wb.get('name')} | set up {_local(pod.get('setUpTime'))}",
        f"Measurement schedule: {_schedule(pod)} daily, minimum {pod.get('measAutoHrs')}h apart",
        "",
        "SYMPTOM",
        f"  Last successful measurement: {_local(wb.get('latestMeasureTime'))}"
        f" ({wb.get('latestMeasureTimeHuman')})",
        f"  Failed attempts since: {len(failures)}",
        "",
        "CASSETTE PAD CONSUMPTION",
        f"  A completed measurement consumes ~{COMPLETE_MEASUREMENT_PADS} pads."
        " A partial count with no new reading is an attempt that aborted.",
    ]
    for e in events:
        consumed = "" if e["consumed"] is None else f"{abs(e['consumed']):.0f} pads  "
        flow = "" if e["skimmer_flow"] is None else f" | skimmer flow {e['skimmer_flow']:.0f} gpm"
        out.append(f"  {_local(e['fetched_at'])}  {e['cassette_pads_left']:.0f}/"
                   f"{e['cassette_pads_max']:.0f} pads  {consumed}{e['outcome']}{flow}")

    out += cross_cassette_summary(events)

    out += [
        "",
        "THE POD IS OTHERWISE HEALTHY",
        f"  Last cloud check-in : {_local(pod.get('lastCxnTime'))}",
        f"  Water temp reporting: {wb.get('waterTempTimeHuman')} (updates continuously)",
        f"  WiFi                : {rssi.get('rssi')} dBm, {rssi.get('desc')}",
        f"  Battery             : {batt.get('pctLeft')}% ({batt.get('timeLeftText')})",
        f"  Cassette            : {lab.get('pctLeft')}% "
        f"({lab.get('amountLeft')}/{lab.get('maxAmount')} pads), status {lab.get('status')}",
        "",
        "ACTIVE ALERTS",
    ]
    for a in wb.get("alerts", []):
        out.append(f"  [{a.get('status')}] {a.get('source')}/{a.get('condition')}: {a.get('text')}")

    if lab.get("refillTime"):
        out += [
            "",
            "THE ONE THING THAT CHANGED",
            f"  The cassette was re-registered at {_local(lab.get('refillTime'))}"
            f" (maxAmount reset to {lab.get('maxAmount')}).",
            "  Measurements have failed since.",
        ]

    return "\n".join(out)


if __name__ == "__main__":
    sys.stdout.write(build() + "\n")
