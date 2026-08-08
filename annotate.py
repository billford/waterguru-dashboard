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


def cmd_water_stopped(args):
    """Closes the most recent open top-up, making it a window rather than a point."""
    events = load_events()
    open_fills = {
        when: e for when, e in events.items()
        if e.get("type") == "water_added" and not e.get("ends_at")
    }
    if not open_fills:
        print("No open top-up to close. Record one with `water-added` first.", file=sys.stderr)
        sys.exit(1)

    started = max(open_fills)
    when = args.timestamp or datetime.now(timezone.utc).isoformat()
    events[started]["ends_at"] = when

    duration = _parse_iso(when) - _parse_iso(started)
    hours = duration.total_seconds() / 3600
    save_events(events)
    print(f"Top-up closed: {started[:16]} to {when[:16]} ({hours:.1f}h).")
    print("Readings overlapping that whole window are excluded from the burn-rate fit.")


def _parse_iso(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


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

    p = sub.add_parser("water-stopped", help="close the current top-up")
    p.add_argument("timestamp", nargs="?", help="when, ISO format; defaults to now")
    p.set_defaults(func=cmd_water_stopped)

    p = sub.add_parser("handtest", help="record a manual test-kit result")
    for flag, (field, unit) in HANDTEST_FIELDS.items():
        p.add_argument(f"--{flag}", type=float,
                       help=f"{field.replace('_', ' ')}" + (f" ({unit})" if unit else ""))
    p.add_argument("--kit", help="which kit, e.g. 'Taylor K-2006'")
    p.add_argument("--at", help="when, ISO format; defaults to now")
    p.add_argument("--note", help="anything worth remembering about the test")
    p.set_defaults(func=cmd_handtest)

    p = sub.add_parser("note", help="log a free-text entry against the pool")
    p.add_argument("text")
    p.add_argument("--at", help="when, ISO format; defaults to now")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("why", help="attach a reason to a detected equipment change")
    p.add_argument("change", help="change id (see `log`) or a timestamp prefix")
    p.add_argument("text")
    p.set_defaults(func=cmd_why)

    sub.add_parser("log", help="timeline of equipment changes and notes").set_defaults(func=cmd_log)

    p = sub.add_parser("clear", help="remove a manual verdict")
    p.add_argument("timestamp")
    p.set_defaults(func=cmd_clear)

    args = parser.parse_args()
    args.func(args)




# What a liquid kit measures, mapped to the sensor's own field names so the two
# can be compared directly.
HANDTEST_FIELDS = {
    "fc": ("free_cl", "ppm"), "ph": ("ph", ""), "ta": ("ta", "ppm"),
    "ch": ("ch", "ppm"), "cya": ("cya", "ppm"), "salt": ("salt", "ppm"),
}


def cmd_handtest(args):
    """Records a manual test result alongside the sensor's own readings.

    A drop-count titration is more trustworthy than the pod when the two
    disagree, and it is the only chemistry available at all while the pod is
    failing to measure. Recording it means the dashboard has something to show,
    and that a disagreement is visible rather than remembered.
    """
    values = {}
    for flag, (field, _unit) in HANDTEST_FIELDS.items():
        value = getattr(args, flag, None)
        if value is not None:
            values[field] = value
    if not values:
        print("Nothing recorded - pass at least one reading, e.g. --fc 5.2 --ph 7.5",
              file=sys.stderr)
        sys.exit(1)

    events = load_events()
    when = args.at or datetime.now(timezone.utc).isoformat()
    events[when] = {
        "type": "hand_test",
        "values": values,
        "kit": args.kit,
        "note": args.note,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    save_events(events)
    print(f"Recorded hand test at {when[:16]}:")
    for field, value in values.items():
        print(f"   {field:8} = {value}")
    print("Shown on the dashboard beside the sensor's own numbers.")


def cmd_note(args):
    """Free-text log entry - the half of a pool notebook a controller can't infer."""
    events = load_events()
    when = args.at or datetime.now(timezone.utc).isoformat()
    events[when] = {"type": "note", "note": args.text,
                    "recorded_at": datetime.now(timezone.utc).isoformat()}
    save_events(events)
    print(f"Logged at {when}: {args.text}")


def cmd_why(args):
    """Attaches a reason to an automatically-detected equipment change.

    The controller already knows what changed and when; this supplies the only
    part it can't - why you did it.
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, detected_at, description, note FROM system_changes"
            " ORDER BY detected_at DESC LIMIT 20"
        ).fetchall()
        if not rows:
            print("No equipment changes recorded yet.")
            return
        match = next((r for r in rows if str(r["id"]) == str(args.change)), None)
        if match is None:
            match = next((r for r in rows if str(args.change) in (r["detected_at"] or "")), None)
        if match is None:
            print(f"No change matching {args.change!r}. Recent changes:", file=sys.stderr)
            for r in rows[:10]:
                print(f"  [{r['id']}] {r['detected_at'][:16]}  {r['description']}", file=sys.stderr)
            sys.exit(1)
        conn.execute("UPDATE system_changes SET note = ? WHERE id = ?", (args.text, match["id"]))
        conn.commit()
        print(f"[{match['id']}] {match['description']}\n   why: {args.text}")
    finally:
        conn.close()


def cmd_log(args):
    """One timeline: what the controller saw change, and what you wrote down."""
    entries = []

    conn = connect()
    try:
        for r in conn.execute(
            "SELECT id, detected_at, description, note FROM system_changes ORDER BY detected_at"
        ):
            entries.append((r["detected_at"], "equipment", r["description"], r["note"], r["id"]))
    finally:
        conn.close()

    for when, event in load_events().items():
        kind = event.get("type", "note")
        label = "water added" if kind == "water_added" else "note"
        text = event.get("note") or ""
        if event.get("ends_at"):
            hours = (_parse_iso(event["ends_at"]) - _parse_iso(when)).total_seconds() / 3600
            text = f"(ran {hours:.1f}h) {text}".strip()
        entries.append((when, label, text, None, None))

    if not entries:
        print("Nothing logged yet.")
        return

    for when, kind, text, note, cid in sorted(entries, key=lambda e: e[0] or ""):
        tag = f"[{cid}] " if cid else ""
        print(f"{(when or '')[:16]}  {kind:10} {tag}{text}")
        if note:
            print(f"{'':18} {'':10} why: {note}")

if __name__ == "__main__":
    main()
