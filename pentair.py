"""Reads the Pentair IntelliCenter over its local WebSocket API.

The controller runs the whole pool system - pump, gas heater, salt chlorine
generator, lights, water features - and answers on port 6680 with no
authentication to anything on the LAN. That makes it a much richer picture of
the pool than the WaterGuru sensor alone, and it explains things the sensor
can only observe the consequences of.

The most consequential thing it revealed: this pool has an **IntelliChlor salt
cell running at 60%**. Chlorine isn't merely decaying here, it's being generated
whenever the pump runs - which the chlorine forecast previously assumed away.

**Protocol notes**, since the API is undocumented and the shape is not obvious:

- Requests are JSON over a plain WebSocket, each with a `messageID` echoed back.
- `GetQuery/GetHardwareDefinition` returns the object tree, but nested children
  live under `CIRCUITS`, not `OBJLIST` as the top level suggests.
- `GetQuery/GetConfiguration` is what actually lists bodies, circuits and
  features.
- `GetParamList` returns its payload under **`objectList`**, while the
  `GetQuery` commands return theirs under `answer`. Reading the wrong key makes
  a perfectly good 200 response look empty.
- `objnam: "ALL"` and `condition: "OBJTYP=..."` are not supported by this
  firmware (IC 2.019); objects must be named explicitly.

Everything here is read-only. The API would happily let us turn the heater on;
this module deliberately never writes.
"""
import json
import os
import uuid
from datetime import datetime, timezone

import websocket

DEFAULT_PORT = 6680
TIMEOUT_SECONDS = 15

# Object names are stable per installation but not across installations, so
# they're discovered from the configuration rather than hardcoded.
BODY_KEYS = ["SNAME", "STATUS", "TEMP", "LSTTMP", "LOTMP", "HITMP", "HTMODE", "VOL", "HTSRC"]
CHEM_KEYS = ["SNAME", "STATUS", "PRIM", "SEC", "SALT", "SUPER", "TIMOUT"]
HEATER_KEYS = ["SNAME", "STATUS", "SUBTYP", "HTMODE"]
CIRCUIT_KEYS = ["SNAME", "STATUS", "SUBTYP"]

# The controller's own settings object, which carries its timezone offset.
SYSTEM_OBJECT = "_5451"
SYSTEM_KEYS = ["MODE", "VER", "PROPNAME", "TIMZON", "ZIP"]

PUMP_KEYS = ["SNAME", "SUBTYP", "STATUS", "RPM", "GPM", "PWR", "MIN", "MAX", "ALARM"]

# Pumps hang off no body and appear in no configuration query, so they can't be
# discovered the way the heater and chlorinator are - they have to be probed by
# name. These cover the naming schemes seen in the wild; the first that answers
# with OBJTYP=PUMP wins.
PUMP_CANDIDATES = [f"PMP{i:02d}" for i in range(1, 9)] + [f"P{i:04d}" for i in range(1, 9)]

# HTMODE is 0 when the heater isn't currently calling for heat.
HTMODE_IDLE = "0"


class IntelliCenterError(RuntimeError):
    pass


def _endpoint(host: str = None) -> str:
    host = host or os.environ.get("PENTAIR_HOST")
    if not host:
        raise IntelliCenterError("PENTAIR_HOST is not set")
    if ":" not in host:
        host = f"{host}:{DEFAULT_PORT}"
    return f"ws://{host}"


class Client:
    """One short-lived connection. The controller is happy with reconnects."""

    def __init__(self, host: str = None, timeout: int = TIMEOUT_SECONDS):
        self.url = _endpoint(host)
        self.timeout = timeout
        self.ws = None

    def __enter__(self):
        self.ws = websocket.create_connection(self.url, timeout=self.timeout)
        return self

    def __exit__(self, *exc):
        if self.ws:
            self.ws.close()

    def _send(self, message: dict) -> dict:
        message["messageID"] = str(uuid.uuid4())
        self.ws.send(json.dumps(message))
        reply = json.loads(self.ws.recv())
        if str(reply.get("response")) != "200":
            raise IntelliCenterError(f"{message.get('command')} returned {reply.get('response')}")
        return reply

    def query(self, query_name: str) -> list:
        """GetQuery commands answer under `answer`."""
        return self._send(
            {"command": "GetQuery", "queryName": query_name, "arguments": ""}
        ).get("answer", []) or []

    def params(self, requests: list[dict]) -> dict:
        """GetParamList answers under `objectList` - not `answer`."""
        reply = self._send({"command": "GetParamList", "condition": "", "objectList": requests})
        return {
            item.get("objnam"): (item.get("params") or {})
            for item in reply.get("objectList", []) or []
        }


def discover_objects(client: Client) -> dict:
    """Finds the body, heater, chlorinator and circuits for this installation."""
    config = client.query("GetConfiguration")

    found = {"body": None, "heater": None, "chem": None, "circuits": []}
    for item in config:
        params = item.get("params") or {}
        objnam = item.get("objnam")
        objtyp = params.get("OBJTYP")

        if objtyp == "BODY" and found["body"] is None:
            found["body"] = objnam
            # Heater and chlorinator hang off the body.
            for child in params.get("OBJLIST") or []:
                child_params = child.get("params") or {}
                if child_params.get("OBJTYP") == "HEATER":
                    found["heater"] = child.get("objnam")
                elif child_params.get("OBJTYP") == "CHEM":
                    found["chem"] = child.get("objnam")
        elif objtyp == "CIRCUIT":
            found["circuits"].append(objnam)

    found["pumps"] = _discover_pumps(client)
    return found


def _discover_pumps(client: Client) -> list[str]:
    """Probes for pump objects, which no query lists."""
    found = []
    # Batched: one round trip per group, rather than sixteen.
    for i in range(0, len(PUMP_CANDIDATES), 8):
        batch = PUMP_CANDIDATES[i:i + 8]
        params = client.params([{"objnam": o, "keys": ["SUBTYP", "RPM", "SNAME"]} for o in batch])
        for objnam, values in params.items():
            # An object with no value echoes the key name back, so a real pump is
            # one whose RPM is anything other than the literal string "RPM".
            if values.get("RPM") not in (None, "", "RPM"):
                found.append(objnam)
    return found


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_state(host: str = None) -> dict:
    """A normalized snapshot of the pool system."""
    with Client(host) as client:
        objects = discover_objects(client)

        requests = []
        if objects["body"]:
            requests.append({"objnam": objects["body"], "keys": BODY_KEYS})
        if objects["chem"]:
            requests.append({"objnam": objects["chem"], "keys": CHEM_KEYS})
        if objects["heater"]:
            requests.append({"objnam": objects["heater"], "keys": HEATER_KEYS})
        requests += [{"objnam": c, "keys": CIRCUIT_KEYS} for c in objects["circuits"]]
        # The controller knows its own timezone - the clock the pump and
        # measurement schedules already run on, and the most authoritative
        # source for what day it is at the pool.
        requests.append({"objnam": SYSTEM_OBJECT, "keys": SYSTEM_KEYS})
        requests += [{"objnam": p, "keys": PUMP_KEYS} for p in objects.get("pumps", [])]

        params = client.params(requests)

    return normalize(params, objects)


def normalize(params: dict, objects: dict) -> dict:
    """Turns the controller's terse key/value pairs into something readable."""
    body = params.get(objects.get("body")) or {}
    chem = params.get(objects.get("chem")) or {}
    heater = params.get(objects.get("heater")) or {}
    system = params.get(SYSTEM_OBJECT) or {}
    pump = next((params.get(p) or {} for p in objects.get("pumps") or []), {})

    circuits = {}
    for objnam in objects.get("circuits") or []:
        circuit = params.get(objnam)
        if circuit and circuit.get("SNAME"):
            circuits[circuit["SNAME"]] = circuit.get("STATUS") == "ON"

    # The filter circuit being ON is what tells us the pump is actually running,
    # which is what makes both chlorine generation and sensor sampling possible.
    pump_running = body.get("STATUS") == "ON"

    return {
        "read_at": datetime.now(timezone.utc).isoformat(),
        "water_temp": _as_float(body.get("TEMP")),
        "body_name": body.get("SNAME"),
        "last_temp": _as_float(body.get("LSTTMP")),
        "setpoint": _as_float(body.get("LOTMP")),
        "max_setpoint": _as_float(body.get("HITMP")),
        "volume_gallons": _as_float(body.get("VOL")),
        "pump_running": pump_running,
        "heater_enabled": heater.get("STATUS") == "ON",
        # HTMODE reports whether the heater is *currently firing*, which is not
        # the same as being enabled - it idles once the setpoint is reached.
        "heater_calling": body.get("HTMODE") not in (None, HTMODE_IDLE),
        "heater_name": heater.get("SNAME"),
        "chlorinator_output_pct": _as_float(chem.get("PRIM")),
        "chlorinator_spa_pct": _as_float(chem.get("SEC")),
        "salt_ppm": _as_float(chem.get("SALT")),
        "chlorinator_name": chem.get("SNAME"),
        # The only live equipment telemetry the controller offers: actual flow,
        # actual speed, actual power. Everything else is a setting or a state.
        "pump_name": pump.get("SNAME"),
        "pump_rpm": _as_float(pump.get("RPM")),
        "pump_gpm": _as_float(pump.get("GPM")),
        "pump_watts": _as_float(pump.get("PWR")),
        "pump_alarm": pump.get("ALARM") not in (None, "", "OFF", "ALARM"),
        "utc_offset_hours": _as_float(system.get("TIMZON")),
        "zip": system.get("ZIP"),
        "circuits": circuits,
    }


def store_state(state: dict, conn=None):
    """Appends a system snapshot. Its own table - different device, different clock."""
    from db import connect

    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            """INSERT INTO system_snapshots (
                   read_at, water_temp, setpoint, volume_gallons, pump_running,
                   heater_enabled, heater_calling, chlorinator_output_pct, salt_ppm,
                   pump_rpm, pump_gpm, pump_watts, circuits_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                state["read_at"], state.get("water_temp"), state.get("setpoint"),
                state.get("volume_gallons"), int(bool(state.get("pump_running"))),
                int(bool(state.get("heater_enabled"))), int(bool(state.get("heater_calling"))),
                state.get("chlorinator_output_pct"), state.get("salt_ppm"),
                state.get("pump_rpm"), state.get("pump_gpm"), state.get("pump_watts"),
                json.dumps(state.get("circuits") or {}),
            ),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


# Enough poll samples to trust a measured duty cycle over a declared one.
MIN_SAMPLES_FOR_RUNTIME = 72


def declared_runtime_fraction() -> float | None:
    """Pump runtime as configured by hand, e.g. PUMP_RUNTIME_HOURS=23.75.

    Measuring beats being told, but a schedule the owner can read off the
    controller is a real fact, and waiting days to rediscover it is silly.
    """
    raw = os.environ.get("PUMP_RUNTIME_HOURS")
    if not raw:
        return None
    try:
        return min(1.0, max(0.0, float(raw) / 24.0))
    except ValueError:
        return None


# A pump that runs 23h45m a day pauses briefly for a cooldown. A single sample
# taken inside that window says "off", which is true for four more minutes and
# misleading for the twelve hours the dashboard then displays it.
BRIEF_PAUSE_MINUTES = 45


def pump_context(state: dict, history: list[dict]) -> dict:
    """Whether the pump being off right now means anything.

    The twice-daily fetch happens to land inside this pump's cooldown window, so
    the published snapshot claimed the pump was off every morning and stayed
    that way until evening. The ten-minute poll history distinguishes a
    scheduled pause from an actual stop.
    """
    running = bool(state.get("pump_running"))
    if running:
        return {"running": True, "brief_pause": False, "note": None}

    from freshness import parse_ts
    now = parse_ts(state.get("read_at"))
    recent_run = None
    for row in history:                       # history is newest-first
        if row.get("pump_running") and parse_ts(row.get("read_at")):
            recent_run = parse_ts(row["read_at"])
            break

    if now is None or recent_run is None:
        return {"running": False, "brief_pause": False,
                "note": "The pump is off and there is no recent record of it running."}

    minutes = (now - recent_run).total_seconds() / 60
    if minutes <= BRIEF_PAUSE_MINUTES:
        return {
            "running": False,
            "brief_pause": True,
            "minutes_since_running": round(minutes),
            "note": f"The pump is paused - it was running {round(minutes)} minutes ago. "
                    "This pool runs almost continuously with a short daily cooldown, so a "
                    "brief stop is the schedule rather than a fault.",
        }
    return {
        "running": False,
        "brief_pause": False,
        "minutes_since_running": round(minutes),
        "note": f"The pump has been off for about {round(minutes / 60)} hours. Nothing "
                "circulates, filters or chlorinates while it is stopped.",
    }


def pump_runtime_fraction(rows: list[dict]) -> float | None:
    """Rough share of recent readings with the pump running.

    A stand-in for duty cycle until there's enough history to do better. It
    matters because the salt cell only generates chlorine while water moves,
    so an output percentage means nothing without knowing how long it ran.
    """
    if not rows:
        return None
    return round(sum(1 for r in rows if r.get("pump_running")) / len(rows), 2)


# Pentair's published figures for IntelliChlor, rather than the round numbers I
# picked before reading the manual. Ideal 3200-3400; the cell operates from 2700
# to 4500 and suspends production above 4500 to protect itself.
SALT_IDEAL_LOW = 3200
SALT_IDEAL_HIGH = 3400
SALT_OPERATING_MIN = 2700
SALT_OPERATING_MAX = 4500

# A completed measurement consumes this many pads - observed directly on the one
# that succeeded (192 -> 182). Fewer, with no new reading, is an aborted attempt.
COMPLETE_MEASUREMENT_PADS = 10

# Output this high usually means the cell is compensating for something else -
# most often stabilizer too low to protect the chlorine it makes.
HIGH_OUTPUT_PCT = 50


# Volumes disagreeing by more than this changes doses enough to matter.
VOLUME_MISMATCH_TOLERANCE = 0.10


def volume_mismatch(controller_gallons, waterguru_gallons) -> str | None:
    """Flags the two systems disagreeing about how big the pool is.

    This is the one cross-device discrepancy that can do real damage. WaterGuru
    sizes its dose recommendations from its configured volume, so if that figure
    is wrong every dose it suggests is wrong by the same proportion - and the
    doses are things like "add 73 cups of calcium chloride", where a third too
    much is a genuine overshoot that then has to be diluted back out.

    The controller's figure isn't automatically right either; it's whatever was
    entered at installation. The point is to surface the disagreement, not to
    pick a winner.
    """
    if not controller_gallons or not waterguru_gallons:
        return None

    ratio = waterguru_gallons / controller_gallons
    if abs(ratio - 1) <= VOLUME_MISMATCH_TOLERANCE:
        return None

    direction = "high" if ratio > 1 else "low"
    return (
        f"The controller says this pool is {controller_gallons:,.0f} gallons; WaterGuru is "
        f"configured for {waterguru_gallons:,.0f}. Every dose WaterGuru recommends is sized "
        f"from its own figure, so if the controller is right they are all about "
        f"{abs(ratio - 1) * 100:.0f}% too {direction}. Worth settling which is correct before "
        "adding anything by the cupful."
    )


def system_note(state: dict, cya: float = None) -> str | None:
    """The thing worth saying about how the equipment is being asked to run.

    A salt cell working hard is a symptom, not a setting. Chlorine that
    disappears as fast as it's generated is usually a stabilizer problem, and
    turning the cell up treats it by burning the cell out faster.
    """
    notes = []

    salt = state.get("salt_ppm")
    if salt is not None and salt >= SALT_OPERATING_MAX:
        notes.append(
            f"Salt is {salt:g} ppm, at or above the {SALT_OPERATING_MAX} ppm point where "
            "Pentair says the cell suspends production to protect itself. It comes down only "
            "by dilution."
        )
    elif salt is not None and salt > SALT_IDEAL_HIGH:
        notes.append(
            f"Salt is {salt:g} ppm, above Pentair's ideal {SALT_IDEAL_LOW}-{SALT_IDEAL_HIGH} ppm "
            f"but inside the operating range, which runs to {SALT_OPERATING_MAX}. Higher doesn't "
            "sanitize better and accelerates corrosion; it dilutes down as water is topped up."
        )
    elif salt is not None and salt < SALT_OPERATING_MIN:
        notes.append(
            f"Salt is {salt:g} ppm, below the {SALT_OPERATING_MIN} ppm operating minimum - "
            "the cell will underproduce until it's raised."
        )

    output = state.get("chlorinator_output_pct")
    if output is not None and output >= HIGH_OUTPUT_PCT and cya is not None and cya < 30:
        notes.append(
            f"The cell is running at {output:g}% while stabilizer sits at {cya:g} ppm. "
            "Chlorine is being generated hard and destroyed by sunlight almost as fast; "
            "raising CYA does more than raising output."
        )

    return " ".join(notes) or None


def check_dilution(conn, state: dict) -> dict | None:
    """Infers a top-up from a fall in salt, and records it like a manual one.

    Salt can only be diluted, never consumed - so this closes the loop that
    previously depended on remembering to run `annotate.py water-added`, which
    is exactly the kind of thing nobody remembers at the poolside.
    """
    import dilution
    import trust

    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT read_at, salt_ppm FROM system_snapshots"
            " WHERE salt_ppm IS NOT NULL ORDER BY read_at"
        ).fetchall()
    ]
    before, after = dilution.recent_salt_pair(rows)
    found = dilution.detect_top_up(before, after, state.get("volume_gallons"))
    if not found:
        return None

    # Recorded as a normal event so the chlorine fit excludes the interval,
    # flagged as inferred so it is distinguishable from something you observed.
    events = trust.load_events()
    already = any(
        e.get("type") == "water_added" and e.get("source") == "salt"
        and e.get("from_salt") == found["from_salt"]
        for e in events.values()
    )
    if already:
        return None

    events[state["read_at"]] = {
        "type": "water_added",
        "source": "salt",
        "note": found["text"],
        "gallons_added": found["gallons_added"],
        "from_salt": found["from_salt"],
        "recorded_at": state["read_at"],
    }
    trust.save_events(events)
    return found


def _interventions(state: dict, days: int = 30) -> dict:
    """Chemistry that moved in a direction the pool cannot move on its own.

    Kept factual: it reports what the numbers show and converts to familiar
    units. Whether an addition was warranted is a question only the reading it
    was responding to can answer.
    """
    import interventions
    import saltcell
    from db import connect, dedupe_by_measurement

    conn = connect()
    try:
        system_rows = [
            dict(r) for r in conn.execute(
                "SELECT read_at, salt_ppm FROM system_snapshots"
                " WHERE read_at >= datetime('now', ?) ORDER BY read_at", (f"-{days} days",)
            ).fetchall()
        ]
        chem_rows = [
            dict(r) for r in conn.execute(
                "SELECT fetched_at, latest_measure_time, free_cl FROM snapshots"
                " WHERE fetched_at >= datetime('now', ?) ORDER BY fetched_at", (f"-{days} days",)
            ).fetchall()
        ]
    finally:
        conn.close()

    gallons = state.get("volume_gallons")
    # The generous bound: what the cell could add at *full* output, so only a
    # rise it could not possibly account for is reported.
    max_generation = saltcell.generation_ppm_per_day(gallons, 100.0, 1.0) or 0.0

    salt = interventions.detect_salt_addition(system_rows, gallons)
    chlorine = interventions.detect_chlorine_addition(
        dedupe_by_measurement(chem_rows), max_generation, gallons)

    return {
        "salt": salt,
        "chlorine": chlorine,
        "summary": interventions.summarize(salt, chlorine, days),
        "window_days": days,
    }


def export_system(out_path, host: str = None, lookback: int = 200) -> dict | None:
    """Reads, stores and publishes the system snapshot for the dashboard."""
    from db import connect

    state = read_state(host)

    conn = connect()
    try:
        record_changes(state, conn)
        state["dilution_detected"] = check_dilution(conn, state)
        store_state(state, conn)
        history = [
            dict(r)
            for r in conn.execute(
                "SELECT read_at, pump_running, heater_calling, water_temp, salt_ppm,"
                " pump_rpm, pump_gpm, pump_watts"
                " FROM system_snapshots ORDER BY read_at DESC LIMIT ?",
                (lookback,),
            ).fetchall()
        ]
    finally:
        conn.close()

    payload_pump = pump_context(state, history)
    measured = pump_runtime_fraction(history) if len(history) >= MIN_SAMPLES_FOR_RUNTIME else None
    declared = declared_runtime_fraction()

    payload = dict(state)
    payload["pump_runtime_fraction"] = measured if measured is not None else declared
    payload["runtime_source"] = (
        "measured" if measured is not None else ("declared" if declared is not None else None)
    )
    payload["history_points"] = len(history)
    cya = None
    try:
        conn = connect()
        row = conn.execute(
            "SELECT cya FROM snapshots WHERE cya IS NOT NULL ORDER BY fetched_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        cya = row["cya"] if row else None
    except Exception:
        cya = None
    payload["pump_context"] = payload_pump
    payload["system_note"] = system_note(state, cya)

    import restriction
    payload["restriction"] = restriction.status(history)
    payload["interventions"] = _interventions(state)

    # Cross-check the two systems' idea of pool volume, since WaterGuru's dose
    # recommendations are computed from its own figure.
    wg_gallons = None
    try:
        raw = json.loads((out_path.parent.parent.parent / "data" / "latest.json").read_text())
        wg_gallons = raw["data"]["waterBodies"][0]["waterBody"].get("sizeGallons")
    except Exception:
        wg_gallons = None
    payload["waterguru_gallons"] = wg_gallons
    payload["volume_mismatch"] = volume_mismatch(state.get("volume_gallons"), wg_gallons)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    return payload



# Settings a person changes, as opposed to state that moves on its own. The pump
# and circuits switch constantly under their schedule, so logging those would
# bury the handful of entries that actually represent a decision.
WATCHED_SETTINGS = {
    "chlorinator_output_pct": ("Salt cell output", "%"),
    "setpoint": ("Heater setpoint", "°F"),
    "heater_enabled": ("Heater", ""),
    "volume_gallons": ("Configured pool volume", " gal"),
}

# Salt drifts continuously with evaporation and top-ups; only a real step change
# is worth an entry.
SALT_STEP_PPM = 150


def _fmt(field, value):
    label, unit = WATCHED_SETTINGS.get(field, (field, ""))
    if isinstance(value, bool):
        return "on" if value else "off"
    if value is None:
        return "unknown"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value}{unit}"


def detect_changes(previous: dict, current: dict) -> list[dict]:
    """Settings that moved between two controller reads.

    Answers "what did I change, and when?" without anyone having to remember to
    write it down - which is the half of a pool notebook that never survives
    contact with actually being at the pool.
    """
    if not previous:
        return []

    changes = []
    for field, (label, _unit) in WATCHED_SETTINGS.items():
        old, new = previous.get(field), current.get(field)
        if old is None or new is None or old == new:
            continue
        # The heater follows the pump: it drops out during the daily cooldown
        # and comes back after, producing two entries a day that record the
        # schedule rather than a decision. Burying the real entries is exactly
        # what this log exists to avoid.
        if field == "heater_enabled" and current.get("pump_running") is False:
            continue
        changes.append({
            "field": field,
            "old_value": str(old),
            "new_value": str(new),
            "description": f"{label} changed from {_fmt(field, old)} to {_fmt(field, new)}",
        })

    old_salt, new_salt = previous.get("salt_ppm"), current.get("salt_ppm")
    if old_salt is not None and new_salt is not None and abs(new_salt - old_salt) >= SALT_STEP_PPM:
        direction = "rose" if new_salt > old_salt else "fell"
        changes.append({
            "field": "salt_ppm",
            "old_value": str(old_salt),
            "new_value": str(new_salt),
            "description": f"Salt {direction} from {old_salt:g} to {new_salt:g} ppm",
        })

    return changes


def record_changes(current: dict, conn=None) -> list[dict]:
    """Compares against the previous read and logs anything that moved."""
    from db import connect

    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            """SELECT chlorinator_output_pct, setpoint, heater_enabled, volume_gallons, salt_ppm
               FROM system_snapshots ORDER BY read_at DESC LIMIT 1"""
        ).fetchone()
        previous = dict(row) if row else None
        if previous:
            previous["heater_enabled"] = bool(previous["heater_enabled"])

        changes = detect_changes(previous, current)
        for change in changes:
            conn.execute(
                """INSERT INTO system_changes (detected_at, field, old_value, new_value, description)
                   VALUES (?, ?, ?, ?, ?)""",
                (current["read_at"], change["field"], change["old_value"],
                 change["new_value"], change["description"]),
            )
        conn.commit()
        return changes
    finally:
        if own:
            conn.close()


def export_log(out_path, limit: int = 40) -> dict:
    """The pool log: what the controller saw change, and what was written down.

    Published so it's readable from a phone at the poolside, which is where the
    question "why did I change that?" actually gets asked.
    """
    from db import connect
    from trust import load_events

    entries = []
    conn = connect()
    try:
        for r in conn.execute(
            "SELECT id, detected_at, description, note FROM system_changes"
            " ORDER BY detected_at DESC LIMIT ?", (limit,)
        ):
            entries.append({"at": r["detected_at"], "kind": "equipment",
                            "text": r["description"], "note": r["note"]})
    finally:
        conn.close()

    for when, event in load_events().items():
        kind = event.get("type", "note")
        text = event.get("note") or ("Water added" if kind == "water_added" else "")
        if event.get("ends_at"):
            from freshness import parse_ts
            began, ended = parse_ts(when), parse_ts(event["ends_at"])
            if began and ended:
                text = f"Water added over {(ended - began).total_seconds() / 3600:.1f}h - {text}"
        entries.append({
            "at": when,
            "kind": "water" if kind == "water_added" else "note",
            "text": text,
            "note": None,
        })

    entries.sort(key=lambda e: e["at"] or "", reverse=True)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "entries": entries[:limit]}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    return payload

if __name__ == "__main__":
    from pathlib import Path

    from config import load_dotenv

    load_dotenv()
    print(json.dumps(export_system(Path(__file__).resolve().parent / "site" / "data" / "system.json"), indent=1))
