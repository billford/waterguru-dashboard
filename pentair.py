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

        params = client.params(requests)

    return normalize(params, objects)


def normalize(params: dict, objects: dict) -> dict:
    """Turns the controller's terse key/value pairs into something readable."""
    body = params.get(objects.get("body")) or {}
    chem = params.get(objects.get("chem")) or {}
    heater = params.get(objects.get("heater")) or {}

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
                   heater_enabled, heater_calling, chlorinator_output_pct, salt_ppm, circuits_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                state["read_at"], state.get("water_temp"), state.get("setpoint"),
                state.get("volume_gallons"), int(bool(state.get("pump_running"))),
                int(bool(state.get("heater_enabled"))), int(bool(state.get("heater_calling"))),
                state.get("chlorinator_output_pct"), state.get("salt_ppm"),
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


def pump_runtime_fraction(rows: list[dict]) -> float | None:
    """Rough share of recent readings with the pump running.

    A stand-in for duty cycle until there's enough history to do better. It
    matters because the salt cell only generates chlorine while water moves,
    so an output percentage means nothing without knowing how long it ran.
    """
    if not rows:
        return None
    return round(sum(1 for r in rows if r.get("pump_running")) / len(rows), 2)


# IntelliChlor cells want roughly 3000-3500 ppm; much above that offers no
# benefit and accelerates corrosion, and the cell itself will fault out high.
SALT_HIGH_PPM = 4000
SALT_LOW_PPM = 2800

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
    if salt is not None and salt > SALT_HIGH_PPM:
        notes.append(
            f"Salt is {salt:g} ppm, above the ~3000-3500 ppm these cells want. "
            "Too high doesn't sanitize better - it corrodes fittings and can fault the cell. "
            "It comes down by dilution, so it corrects itself as water is topped up."
        )
    elif salt is not None and salt < SALT_LOW_PPM:
        notes.append(f"Salt is low at {salt:g} ppm; the cell will underproduce until it's raised.")

    output = state.get("chlorinator_output_pct")
    if output is not None and output >= HIGH_OUTPUT_PCT and cya is not None and cya < 30:
        notes.append(
            f"The cell is running at {output:g}% while stabilizer sits at {cya:g} ppm. "
            "Chlorine is being generated hard and destroyed by sunlight almost as fast; "
            "raising CYA does more than raising output."
        )

    return " ".join(notes) or None


def export_system(out_path, host: str = None, lookback: int = 200) -> dict | None:
    """Reads, stores and publishes the system snapshot for the dashboard."""
    from db import connect

    state = read_state(host)

    conn = connect()
    try:
        record_changes(state, conn)
        store_state(state, conn)
        history = [
            dict(r)
            for r in conn.execute(
                "SELECT read_at, pump_running, heater_calling, water_temp, salt_ppm"
                " FROM system_snapshots ORDER BY read_at DESC LIMIT ?",
                (lookback,),
            ).fetchall()
        ]
    finally:
        conn.close()

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
    payload["system_note"] = system_note(state, cya)

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
        entries.append({
            "at": when,
            "kind": "water" if kind == "water_added" else "note",
            "text": event.get("note") or ("Water added" if kind == "water_added" else ""),
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
