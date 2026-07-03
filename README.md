# ACC Telemetry & Virtual Race Engineer

Captures telemetry from **Assetto Corsa Competizione**, prints a live "Race
Engineer" coaching feed, stores everything in a local SQLite database, and
serves a dashboard for post-session analysis. It also runs fully offline with a
built-in simulator, so you can try it without the game.

```
ACC (or simulator) ─▶ logger.py ─▶ live console feed
                                 └▶ telemetry.db ─▶ dashboard.py
```

## Files

- `logger.py` — captures telemetry, prints the live feed, writes `telemetry.db`.
- `acc_telemetry.py` — the data source: ACC shared-memory reader (Windows) and the simulator.
- `dashboard.py` — Streamlit dashboard (Tactical + Strategic views).
- `requirements.txt` — dashboard dependencies.

## Setup

Install [Python 3](https://www.python.org/downloads/) (on Windows, tick "Add
Python to PATH"). The logger needs no extra packages. For the dashboard:

```
python -m pip install -r requirements.txt
```

> On macOS use `python3` instead of `python`.

## Run it (no game needed)

The simulator generates realistic laps so you can test the whole thing:

```
python logger.py --sim
```

Watch the live feed in that terminal. Press `Ctrl-C` to stop.

## Run it with ACC (Windows)

1. Launch ACC and get out on track.
2. Verify the connection first — reads one frame and prints it, writes nothing:

   ```
   python logger.py --check
   ```

   Compare the printed speed / gear / fuel to your in-game HUD. If they match,
   you're good. If they're garbage, the shared-memory layout needs updating.

3. Start logging:

   ```
   python logger.py
   ```

   Leave it running while you drive. `Ctrl-C` stops it and prints a stint summary.

## Open the dashboard

In a second terminal (leave the logger running), pointing at the same database:

```
python -m streamlit run dashboard.py -- --db telemetry.db
```

It opens in your browser at `localhost:8501`.

- **Tactical View** — braking events, lockups, thermal breaches, and an **Event
  detail** panel showing the raw telemetry traces (brake/steer/slip/speed/temps)
  for the selected event.
- **Strategic View** — lap times, fuel burn vs. pit window, and an Endurance
  Management Score for consistency.
- **Live auto-refresh** (sidebar, on by default) re-reads the database every few
  seconds so it updates as you drive.

## Options

```
--sim              use the simulator instead of live ACC
--check            read one live frame, print it, and exit
--db PATH          database file (default: telemetry.db)
--laps N           stop after N laps
--track NAME       simulator track: spa or nordschleife (default: spa)
--hz N             poll rate; also the simulator's playback speed (default: 20)
--seed N           simulator seed for repeatable runs
```

The simulator runs in real time at `--hz 20` (a Spa lap takes ~2:18). Raise
`--hz` to speed up testing; event timing stays correct either way.

## Database

`telemetry.db` holds four tables: **Sessions** (one per stint), **Laps**,
**Events** (`BRAKING` / `FUEL_SAVE` summaries), and **Samples** (the raw ~20Hz
frames captured around each event, for detailed analysis). Query it directly if
you like:

```
sqlite3 telemetry.db "SELECT phase,t_ms,brake,steer,speed_kmh,slip_fl FROM Samples WHERE event_id=1;"
```

## Notes

- Corner names are labelled from track profiles in `acc_telemetry.py`
  (Spa and Nordschleife built in). Other tracks log correctly but use fallback
  labels until a profile is added.
- Detection thresholds (braking, lockup, coast, etc.) live at the top of
  `logger.py` and are easy to tune.
