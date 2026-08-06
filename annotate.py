#!/usr/bin/env python3
"""Mark a measurement as suspect or trusted by hand.

For everything the automatic checks can't see. If you hand-test the water and
the sensor disagrees, you're right and it isn't - and a reading you mark suspect
stops feeding the chlorine forecast, the trend summary, and any dosing advice.

    python annotate.py list
    python annotate.py suspect 2026-08-05T21:10 --note "pump was dead, hand test in range"
    python annotate.py trust 2026-08-06T20:56
    python annotate.py clear 2026-08-05T21:10

Timestamps are matched by prefix, so `2026-08-05T21:10` finds
`2026-08-05T21:10:04.000Z` - no need to transcribe one exactly.
"""
import argparse
import sys
from datetime import datetime, timezone

from db import connect
from trust import evaluate, load_annotations, load_events, save_annotations, save_events


def _measurements(limit=25):
    conn = connect()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                """SELECT fetched_at, latest_measure_time, free_cl, ph, ta, ch, cya, th, alerts_json
                   FROM snapshots ORDER BY fetched_at"""
            ).fetchall()
        ]
    finally:
        conn.close()
    return rows


def cmd_list(args):
    rows = _measurements()
    verdicts = evaluate(rows)
    if not verdicts:
        print("No measurements recorded yet.")
        return

    seen = set()
    for row in rows:
        key = row.get("latest_measure_time") or row.get("fetched_at")
        if key in seen:
            continue
        seen.add(key)
        v = verdicts[key]
        mark = "OK     " if v["trusted"] else "SUSPECT"
        source = f"({v['source']})"
        print(f"{mark} {key}  free_cl={row['free_cl']} ph={row['ph']} {source}")
        for reason in v["reasons"]:
            print(f"          {reason}")

    events = load_events()
    if events:
        print("\nEvents:")
        for when, event in sorted(events.items()):
            note = f" - {event['note']}" if event.get("note") else ""
            print(f"  {when}  {event.get('type')}{note}")


def _set_verdict(key: str, verdict: str, note: str | None):
    annotations = load_annotations()
    annotations[key] = {
        "verdict": verdict,
        "note": note,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    save_annotations(annotations)
    print(f"Marked {key} as {verdict}." + (f" ({note})" if note else ""))
    print("Takes effect on the next run, or run publish.py to refresh now.")


def cmd_suspect(args):
    _set_verdict(args.timestamp, "suspect", args.note)


def cmd_trust(args):
    _set_verdict(args.timestamp, "trusted", args.note)


def cmd_water_added(args):
    """Records a top-up, so the resulting dilution isn't fitted as chlorine demand."""
    events = load_events()
    when = args.timestamp or datetime.now(timezone.utc).isoformat()
    events[when] = {
        "type": "water_added",
        "note": args.note,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    save_events(events)
    print(f"Recorded water added at {when}." + (f" ({args.note})" if args.note else ""))
    print("Readings spanning this point won't be used to fit the chlorine burn rate.")


def cmd_clear(args):
    annotations = load_annotations()
    if args.timestamp not in annotations:
        print(f"No annotation stored for {args.timestamp}.", file=sys.stderr)
        sys.exit(1)
    del annotations[args.timestamp]
    save_annotations(annotations)
    print(f"Cleared the annotation on {args.timestamp}; automatic checks apply again.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show every measurement and its trust verdict").set_defaults(func=cmd_list)

    for name, help_text, func in (
        ("suspect", "exclude a measurement from models and advice", cmd_suspect),
        ("trust", "override an automatic suspect verdict", cmd_trust),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("timestamp", help="measurement time, or any unique prefix of it")
        p.add_argument("--note", help="why - shown on the dashboard")
        p.set_defaults(func=func)

    p = sub.add_parser("water-added", help="record a top-up so dilution isn't read as chlorine demand")
    p.add_argument("timestamp", nargs="?", help="when, ISO format; defaults to now")
    p.add_argument("--note", help="e.g. how much, or why")
    p.set_defaults(func=cmd_water_added)

    p = sub.add_parser("clear", help="remove a manual verdict")
    p.add_argument("timestamp")
    p.set_defaults(func=cmd_clear)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
