#!/usr/bin/env python3
"""Samples the pool controller frequently, to measure what the pump actually does.

The WaterGuru fetch runs twice a day, which is a hopeless basis for working out
pump duty cycle - two samples can't tell an eight-hour schedule from a
twenty-four-hour one. That matters because a salt cell only makes chlorine while
water is moving, so "60% output" means nothing without knowing the runtime it
was applied over.

The controller is on the LAN, unauthenticated and free to query, so unlike the
WaterGuru API there's no reason to be sparing. Every ten minutes gives runtime
to within a few percent.

This writes to the same system_snapshots table the twice-daily run uses; it
simply adds resolution between those runs. It never touches the WaterGuru API
and never publishes, so it's safe to run as often as you like.

    ./venv/bin/python poll_system.py

Scheduled via com.billfordx.pool-poll.plist.
"""
import sys

from config import load_dotenv
from logs import rotating_logger, trim_launchd_logs
from pentair import IntelliCenterError, read_state, record_changes, store_state

# Python owns this file, so RotatingFileHandler can rename it safely. The plist
# sends launchd's own stdout to /dev/null so it never opens it - see logs.py for
# why that distinction matters.
log = rotating_logger("poll", "poll.log")


def main():
    load_dotenv()
    trim_launchd_logs()
    try:
        state = read_state()
    except (IntelliCenterError, OSError) as e:
        # A poller that shouts on every blip is a poller you turn off. The
        # twice-daily run reports controller problems; this one stays quiet.
        log.warning("pool controller unreachable: %s", e)
        return 1

    # Detect before storing - the comparison needs the previous read to still
    # be the most recent row.
    changes = record_changes(state)
    store_state(state)
    for change in changes:
        log.info("CHANGE: %s", change["description"])
    log.info(
        "pump=%s temp=%s rpm=%s gpm=%s watts=%s cell=%s%% salt=%s",
        "on" if state["pump_running"] else "off", state.get("water_temp"),
        state.get("pump_rpm"), state.get("pump_gpm"), state.get("pump_watts"),
        state.get("chlorinator_output_pct"), state.get("salt_ppm"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
