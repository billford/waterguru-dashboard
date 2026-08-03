"""Fires notifications when a water body's status is RED.

Channels: a native macOS notification (osascript) and a push via ntfy.sh
(https://ntfy.sh/<topic> - no account needed, subscribe in the ntfy app).
"""
import os
import subprocess

import requests


def _mac_notification(title: str, message: str):
    script = (
        f'display notification "{_escape_applescript(message)}" '
        f'with title "{_escape_applescript(title)}" sound name "Basso"'
    )
    subprocess.run(["osascript", "-e", script], check=False)


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", ", ")


def _ntfy_push(topic: str, title: str, message: str):
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "warning,pool"},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"ntfy push failed: {e}")


def check_and_alert(rows: list[dict]):
    ntfy_topic = os.environ.get("NTFY_TOPIC")

    for row in rows:
        name = row["name"] or "Pool"

        for title, message in _status_alerts(row, name) + _cassette_alerts(row, name):
            _mac_notification(title, message)
            if ntfy_topic:
                _ntfy_push(ntfy_topic, title, message)


def _status_alerts(row: dict, name: str) -> list[tuple[str, str]]:
    status = row["status"]
    prev_status = row.get("prev_status")

    if status == "RED":
        title = f"{name}: pool status RED"
        message = _format_alerts(row["alerts_json"]) or "Check the dashboard for details."
        return [(title, message)]
    if prev_status == "RED" and status != "RED":
        return [(f"{name}: back to normal", f"Status is now {status}.")]
    return []


def _cassette_alerts(row: dict, name: str) -> list[tuple[str, str]]:
    status = row.get("cassette_status")
    prev_status = row.get("prev_cassette_status")
    if status is None:
        return []

    # Fire once on the transition into RED (or urgent), not on every subsequent RED reading.
    needs_replacement = status == "RED" or row.get("cassette_urgent")
    was_flagged = prev_status == "RED"

    if needs_replacement and not was_flagged:
        pct = row.get("cassette_pct_left")
        days = row.get("cassette_days_left") or ""
        pct_text = f"{pct:.0f}% left" if pct is not None else ""
        detail = ", ".join(x for x in [pct_text, days] if x)
        return [(f"{name}: replace the cassette", detail or "Cassette is running low.")]

    if prev_status == "RED" and status != "RED":
        return [(f"{name}: cassette replaced", "Cassette level is back to normal.")]

    return []


def _format_alerts(alerts_json: str) -> str:
    import json

    try:
        alerts = json.loads(alerts_json)
    except (TypeError, ValueError):
        return ""
    texts = [a.get("text") for a in alerts if a.get("status") == "RED" and a.get("text")]
    return "; ".join(texts)
