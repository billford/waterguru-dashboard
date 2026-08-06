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
from pentair import IntelliCenterError, read_state, store_state


def main():
    load_dotenv()
    try:
        state = read_state()
    except (IntelliCenterError, OSError) as e:
        # A poller that shouts on every blip is a poller you turn off. The
        # twice-daily run reports controller problems; this one stays quiet.
        print(f"pool controller unreachable: {e}", file=sys.stderr)
        return 1

    store_state(state)
    print(
        f"pump={'on' if state['pump_running'] else 'off'} "
        f"temp={state.get('water_temp')} "
        f"cell={state.get('chlorinator_output_pct')}% "
        f"salt={state.get('salt_ppm')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
