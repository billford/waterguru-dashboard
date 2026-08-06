# Pentair IntelliCenter — local API inventory

Undocumented. This is what **this** installation actually answers, probed
directly (firmware `IC: 2.019`, panel `I5P`, module ver `10.001`). Object names
are per-installation; discover them from `GetConfiguration` rather than
hardcoding.

`ws://<host>:6680`, JSON, no authentication.

## Protocol quirks worth knowing

- `GetParamList` returns its payload under **`objectList`**; `GetQuery` returns
  its own under **`answer`**. Read the wrong key and a perfectly good `200`
  looks empty.
- `objnam: "ALL"` and `condition: "OBJTYP=..."` are **not supported**. Objects
  must be named explicitly.
- When an object has no value for a key it **echoes the key name back as the
  value** (`"VER": "VER"`). Filter those out or they read as data.
- Nested children in `GetHardwareDefinition` live under `CIRCUITS`, not the
  `OBJLIST` the top level suggests.
- Long key lists get truncated — batch requests at ~25 keys.

## Query names that work

| Query | Returns |
|---|---|
| `GetConfiguration` | 13 objects — bodies, circuits, features. **The useful one.** |
| `GetCircuitNames` | 111 preset name strings |
| `GetCircuitTypes` | 14 circuit type definitions |
| `GetHardwareDefinition` | panel → module → body tree |

Everything else tried returns `400`: `GetSchedules`, `GetHistory`,
`GetPumpStatus`, `GetSystemInformation`, `GetAlarmList`, `GetEnergyUsage`,
`GetChemHistory`, and ~25 others. **There is no historical data and no schedule
query** — anything time-series has to be sampled and stored locally.

## Live telemetry (changes minute to minute)

| Object | Key | Example | Meaning |
|---|---|---|---|
| `PMP01` | `RPM` | 2050 | pump speed |
| `PMP01` | `GPM` | 50 | **actual flow** |
| `PMP01` | `PWR` | 609 | **watts drawn** |
| `PMP01` | `STATUS` | 10 | running state |
| `PMP01` | `ALARM` | OFF | pump fault |
| `B1101` | `TEMP` | 86 | water temperature |
| `B1101` | `HTMODE` | 0 | 0 = not calling for heat |
| `CHR01` | `SALT` | 4350 | salt ppm |
| `C*`/`FTR*` | `STATUS` | ON/OFF | circuit state |

## Settings (change when you change them)

| Object | Key | Example | Meaning |
|---|---|---|---|
| `B1101` | `LOTMP` | 81 | **heater setpoint** |
| `B1101` | `HITMP` | 100 | max setpoint |
| `B1101` | `VOL` | 15000 | pool volume (gallons) |
| `B1101` | `HTSRC` / `FILTER` | H0001 / C0006 | linked heater, filter circuit |
| `CHR01` | `PRIM` | 50 | **salt cell output %** (pool) |
| `CHR01` | `SEC` | 20 | salt cell output % (spa) |
| `CHR01` | `SUPER` / `TIMOUT` | PERMIT / 86400 | superchlorinate state |
| `H0001` | `STATUS` | ON | heater enabled |
| `H0001` | `START`/`STOP`/`DLY`/`COOL`/`BOOST` | 6/3/5/OFF/0 | heater cycle params |
| `PMP01` | `MIN`/`MAX` | 450 / 3450 | pump RPM range |
| `C0002` | `LIMIT`/`TIME` | 6 / 720 | light egg-timer |

## Identity and clock

| Object | Keys |
|---|---|
| `_5451` (SYSTEM) | `VER`, `PROPNAME`, `ZIP`, `LOCX`/`LOCY`, `TIMZON`, `MODE`, `SERVICE` |
| `_C10C` (SYSTIM) | `DAY` (`08,06,26`), `MIN` (`16,15,00`), `CLK24A`, `TIMZON` |

`TIMZON` is a **fixed** offset with no daylight-saving information — see
`poolclock.py` for why that makes it a cross-check rather than a source.

## Equipment present

`PMP01` VSF pump · `H0001` Gas Heater · `CHR01` IntelliChlor · `VAL01`/`VAL02`
valves (→ Bubbler1, Deck Jet) · `REM01` iS4 remote · `_A135` Air Sensor
(`STATUS: OK` but `MODE: OFF`, reports no temperature) · `_FEA2` freeze
protection · circuits `C0002`–`C0006`, features `FTR01`–`FTR03`.

## Not available

No history of any kind, no schedules, no energy totals, no per-circuit runtime.
Air sensor is present but not reporting. Anything time-series must be sampled
and stored — which is what `poll_system.py` is for.
