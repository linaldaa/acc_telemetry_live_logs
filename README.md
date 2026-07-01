# ACC Telemetry & Virtual Race Engineer

An event-driven telemetry system for **Assetto Corsa Competizione**. It captures
raw physics from ACC's shared memory at 20Hz, abstracts it into discrete
**Events** via a state machine, stores everything in a relational SQLite
database, prints a live "Virtual Race Engineer" feed to the console, and serves
a post-session dashboard for tactical and strategic analysis.

```
ACC shared memory ──▶ logger.py (20Hz poll → state machine → events)
                          │
                          ├─▶ live console feed  (Virtual Race Engineer)
                          └─▶ telemetry.db (SQLite: Sessions / Laps / Events)
                                      │
                                      └─▶ dashboard.py (Streamlit)
```

## Files

| File | Purpose |
|------|---------|
| `acc_telemetry.py` | Telemetry source layer — ACC shared-memory reader (Windows) **and** a built-in simulator. Both emit a unified `TelemetryFrame`. |
| `logger.py` | Polling loop, event state machine, SQLite storage, live Race Engineer feed. |
| `dashboard.py` | Streamlit post-session dashboard (Tactical + Strategic views). |
| `requirements.txt` | Dependencies (the logger needs none; the dashboard needs pandas + streamlit). |

## Quick start (no game required)

The simulator lets you run the whole pipeline without ACC:

```bash
python logger.py --sim --laps 4          # generate a few laps into telemetry.db
pip install -r requirements.txt          # for the dashboard
streamlit run dashboard.py               # open the dashboard
```

## Running against live ACC (Windows)

Launch ACC and get into a session, then:

```bash
python logger.py                         # reads Local\acpmf_* shared memory at 20Hz
```

Leave it running in the background while you drive. Stop with `Ctrl-C` — it
flushes the current state and prints a stint summary.

### Logger options

```
--sim              use the built-in simulator instead of live shared memory
--db PATH          SQLite database path (default: telemetry.db)
--hz N             polling frequency (default: 20)
--laps N           stop after N laps (handy for the simulator / testing)
--track / --car    simulator track + car labels
--seed N           simulator RNG seed (reproducible runs)
```

### Simulator tracks

The simulator ships with track profiles that set a realistic lap time and the
real corner names used in the feed:

```bash
python logger.py --sim --track spa            # ~2:18 lap, Spa corners
python logger.py --sim --track nordschleife   # ~8:00 lap, Nordschleife corners
```

Unknown track names fall back to the Spa profile. Add more in
`TRACK_PROFILES` in `acc_telemetry.py` — each profile is just a lap time plus a
list of corners (position, name, brake pressure, gear, lockup probability). The
logger labels live ACC events from the same profiles, so the corner names match
whichever track you load. The EMS consistency score uses *relative* lap-time
spread, so it stays comparable across short and long tracks.

## The live feed (Virtual Race Engineer)

As you drive, completed events are parsed into plain-English coaching:

```
> [LAP 4 | 02:14.300] ⚠️  EVENT: Severe Braking Zone (Turn 1).
>    ANALYSIS: FL tyre lockup detected. Peak brake temp: 720°C.
>    RECOMMENDATION: Shift brake bias 1.5% rearward.

> [LAP 4 | 03:45.100] ✅ EVENT: Fuel Save (Sector 2).
>    ANALYSIS: Optimal lift and coast executed (Coast: 450ms).

🏁 --- LAP 4 COMPLETED ---
   LAP TIME: 1:56.400 (Variance vs Stint Avg: +0.2s — slow)
   FUEL BURNED: 2.9L | EST. LAPS REMAINING: 18
```

## Database schema

**Sessions** `(session_id PK, track_name, car_model, date)` — one row per stint.

**Laps** `(lap_id PK, session_id FK, lap_number, lap_time, fuel_used, fuel_end)`
— strategic view; a lap row is opened when the lap begins and finalized on
completion so its events attach to the correct lap.

**Events** `(event_id PK, lap_id FK, event_type, location, peak_value, duration,
detail)` — tactical view; `event_type` is `BRAKING` or `FUEL_SAVE`.

## Dashboard personas

**Module A — Tactical View (Driver):** which laps and corners breached thermal
thresholds (peak brake temp, lockups), so the driver can adjust brake bias or
inputs next stint.

**Module B — Strategic View (Race Engineer):** lap-time trend, fuel burned vs
remaining (pit-window crossover), and an **Endurance Management Score (EMS)**
derived from lap-time standard deviation (100 = metronomic consistency).

## How events are detected (state machine)

The loop never stores raw 20Hz samples. Instead it watches for trigger
conditions and aggregates peaks while the condition holds:

- **Braking event** — opens when `brake > 0.05`; tracks peak brake temp, peak
  tyre temp, max wheel slip, and duration. On release it commits one row,
  flagging *severe* braking, *lockups* (slip > 1.2), and *overheating*.
- **Fuel-save event** — opens when coasting (`gas ≈ 0` and `brake ≈ 0` above
  60 km/h) and commits if the coast lasts longer than 0.3s.

Thresholds live at the top of `logger.py` and are easy to tune.

## Notes

- The logger uses only the Python standard library (`sqlite3`, `ctypes`,
  `mmap`), so capture has no third-party dependencies.
- The shared-memory C struct layouts in `acc_telemetry.py` follow ACC's
  documented `SPageFilePhysics` / `SPageFileGraphic` / `SPageFileStatic` pages.
- Per-frame timing is taken from the telemetry clock when available, so event
  durations stay accurate regardless of loop speed.
```
