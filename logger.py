"""
logger.py
=========
ACC Telemetry & Virtual Race Engineer — ingestion + evaluation engine.

Runs a continuous 20Hz polling loop over a telemetry source (live ACC shared
memory, or the built-in simulator), abstracts raw physics into discrete
*Events* via a state machine, persists Sessions / Laps / Events into a local
SQLite database, and prints a live "Virtual Race Engineer" feed to the console.

Usage
-----
    python logger.py                 # live ACC shared memory (Windows)
    python logger.py --sim           # simulator, runs forever
    python logger.py --sim --laps 5  # simulator, stop after 5 laps
    python logger.py --db telemetry.db --hz 20

Stop any time with Ctrl-C — the current state is flushed before exit.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from statistics import mean, pstdev
from typing import List, Optional

from acc_telemetry import TYRE_LABELS, TelemetryFrame, label_for_position

# ---------------------------------------------------------------------------
# Tunable thresholds (the "logic gates")
# ---------------------------------------------------------------------------
BRAKE_ON = 0.05            # brake input considered "on" above this
SEVERE_BRAKE = 0.80        # peak brake fraction that flags a Severe Braking Zone
LOCKUP_SLIP = 1.20         # wheel slip ratio that indicates a lockup
HOT_BRAKE_TEMP = 680.0     # °C brake-disc temp considered overheating
HOT_TYRE_TEMP = 105.0      # °C tyre surface temp threshold (tactical view)

COAST_MIN_S = 0.30         # min duration of a lift-and-coast to count as fuel save
SLOW_DELTA = 0.20          # lap-time variance band (s) for "on pace" vs flagged

TANK_RESERVE = 2.0         # litres held back when estimating laps remaining


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS Sessions (
    session_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    track_name  TEXT,
    car_model   TEXT,
    date        TEXT
);

CREATE TABLE IF NOT EXISTS Laps (
    lap_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER REFERENCES Sessions(session_id),
    lap_number  INTEGER,
    lap_time    REAL,        -- seconds
    fuel_used   REAL,        -- litres
    fuel_end    REAL         -- litres remaining at lap end
);

CREATE TABLE IF NOT EXISTS Events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    lap_id      INTEGER REFERENCES Laps(lap_id),
    event_type  TEXT,        -- 'BRAKING' | 'FUEL_SAVE'
    location    TEXT,        -- corner / sector label
    peak_value  REAL,        -- peak brake temp (°C) or coast gap (ms)
    duration    REAL,        -- seconds
    detail      TEXT         -- free-form analysis string
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def new_session(self, track: str, car: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO Sessions (track_name, car_model, date) VALUES (?, ?, ?)",
            (track, car, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()
        return cur.lastrowid

    def start_lap(self, session_id: int, lap_number: int) -> int:
        """Create a row for a lap that is just beginning (stats filled later)."""
        cur = self.conn.execute(
            "INSERT INTO Laps (session_id, lap_number, lap_time, fuel_used, fuel_end) "
            "VALUES (?, ?, NULL, NULL, NULL)",
            (session_id, lap_number),
        )
        self.conn.commit()
        return cur.lastrowid

    def finalize_lap(self, lap_id: int, lap_time: float,
                     fuel_used: float, fuel_end: float) -> None:
        self.conn.execute(
            "UPDATE Laps SET lap_time = ?, fuel_used = ?, fuel_end = ? WHERE lap_id = ?",
            (lap_time, fuel_used, fuel_end, lap_id),
        )
        self.conn.commit()

    def insert_event(self, lap_id: Optional[int], event_type: str, location: str,
                     peak_value: float, duration: float, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO Events (lap_id, event_type, location, peak_value, duration, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (lap_id, event_type, location, peak_value, duration, detail),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def fmt_time(seconds: float) -> str:
    if seconds <= 0:
        return "--:--.---"
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m}:{s:06.3f}"


def sector_label(idx: int) -> str:
    return f"Sector {idx + 1}"


# ---------------------------------------------------------------------------
# Live "Virtual Race Engineer" evaluation engine
# ---------------------------------------------------------------------------
class RaceEngineer:
    """Turns committed events into plain-English console insights."""

    def on_braking_event(self, lap_num: int, time_str: str, ev: dict) -> None:
        loc = ev["location"]
        peak_temp = ev["peak_brake_temp"]
        hot_idx = ev["hottest_tyre_idx"]
        if ev["severe"]:
            print(f"> [LAP {lap_num} | {time_str}] ⚠️  EVENT: Severe Braking Zone ({loc}).")
        else:
            print(f"> [LAP {lap_num} | {time_str}] EVENT: Braking Zone ({loc}).")

        if ev["lockup"]:
            print(f">    ANALYSIS: {TYRE_LABELS[hot_idx]} tyre lockup detected. "
                  f"Peak brake temp: {peak_temp:.0f}°C.")
            # Lockup on the front axle → move bias rearward; rear → forward.
            direction = "rearward" if hot_idx in (0, 1) else "forward"
            print(f">    RECOMMENDATION: Shift brake bias 1.5% {direction}.")
        elif peak_temp > HOT_BRAKE_TEMP:
            print(f">    ANALYSIS: Brakes overheating. Peak temp: {peak_temp:.0f}°C.")
            print(f">    RECOMMENDATION: Add more lift-and-coast or cool the discs.")
        else:
            print(f">    ANALYSIS: Clean stop. Peak brake temp: {peak_temp:.0f}°C "
                  f"over {ev['duration']:.2f}s.")
        print()

    def on_fuel_save(self, lap_num: int, time_str: str, ev: dict) -> None:
        print(f"> [LAP {lap_num} | {time_str}] ✅ EVENT: Fuel Save ({ev['location']}).")
        print(f">    ANALYSIS: Optimal lift and coast executed "
              f"(Coast: {ev['duration'] * 1000:.0f}ms).")
        print()

    def on_lap_complete(self, lap_num: int, lap_time: float, stint_avg: Optional[float],
                        fuel_used: float, laps_remaining: Optional[float]) -> None:
        print(f"\n\U0001f3c1 --- LAP {lap_num} COMPLETED ---")
        if stint_avg is not None:
            delta = lap_time - stint_avg
            sign = "+" if delta >= 0 else "-"
            tag = "on pace" if abs(delta) <= SLOW_DELTA else ("slow" if delta > 0 else "quick")
            print(f"   LAP TIME: {fmt_time(lap_time)} "
                  f"(Variance vs Stint Avg: {sign}{abs(delta):.1f}s — {tag})")
        else:
            print(f"   LAP TIME: {fmt_time(lap_time)}")
        rem = f"{laps_remaining:.0f}" if laps_remaining is not None else "n/a"
        print(f"   FUEL BURNED: {fuel_used:.1f}L | EST. LAPS REMAINING: {rem}\n")


# ---------------------------------------------------------------------------
# Main state machine
# ---------------------------------------------------------------------------
class TelemetryLogger:
    def __init__(self, source, db: Database, engineer: RaceEngineer) -> None:
        self.source = source
        self.db = db
        self.eng = engineer

        self.session_id: Optional[int] = None
        self.track_name: str = ""        # set once the session starts
        self.current_lap_id: Optional[int] = None
        self.lap_number = 0
        self.lap_times: List[float] = []

        # Fuel bookkeeping
        self.fuel_at_lap_start: Optional[float] = None

        # Braking event accumulator
        self._braking = False
        self._brake_peak = 0.0
        self._brake_temp_peak = 0.0
        self._brake_dur = 0.0
        self._brake_lockup = False
        self._brake_hot_tyre = 0
        self._brake_hot_tyre_temp = 0.0
        self._brake_start_pos = 0.0

        # Coast/fuel-save accumulator
        self._coasting = False
        self._coast_dur = 0.0
        self._coast_start_pos = 0.0

        self._last_completed_laps = 0
        self._tick = 0.05  # seconds per loop, updated from real hz

    # -- session ----------------------------------------------------------
    def ensure_session(self, f: TelemetryFrame) -> None:
        if self.session_id is None:
            self.session_id = self.db.new_session(f.track, f.car_model)
            self.track_name = f.track
            self._last_completed_laps = f.completed_laps
            self.fuel_at_lap_start = f.fuel
            # Open the first lap immediately so its events get a valid lap_id.
            self.lap_number = 1
            self.current_lap_id = self.db.start_lap(self.session_id, self.lap_number)
            print(f"=== SESSION {self.session_id} START === "
                  f"{f.car_model} @ {f.track}\n")

    # -- event: braking ---------------------------------------------------
    def _update_braking(self, f: TelemetryFrame) -> None:
        if f.brake > BRAKE_ON:
            if not self._braking:
                self._braking = True
                self._brake_peak = 0.0
                self._brake_temp_peak = 0.0
                self._brake_dur = 0.0
                self._brake_lockup = False
                self._brake_hot_tyre = 0
                self._brake_hot_tyre_temp = 0.0
                self._brake_start_pos = f.normalized_car_position
            self._brake_dur += self._tick
            self._brake_peak = max(self._brake_peak, f.brake)
            self._brake_temp_peak = max(self._brake_temp_peak, max(f.brake_temp))
            # Track hottest tyre surface during the stop
            hot_idx = max(range(4), key=lambda i: f.tyre_temp[i])
            if f.tyre_temp[hot_idx] > self._brake_hot_tyre_temp:
                self._brake_hot_tyre_temp = f.tyre_temp[hot_idx]
                self._brake_hot_tyre = hot_idx
            if max(f.wheel_slip) > LOCKUP_SLIP:
                self._brake_lockup = True
                # Attribute lockup to the highest-slip wheel
                self._brake_hot_tyre = max(range(4), key=lambda i: f.wheel_slip[i])
        elif self._braking:
            self._commit_braking(f)

    def _commit_braking(self, f: TelemetryFrame) -> None:
        self._braking = False
        if self._brake_dur < 0.10:  # ignore brushing the pedal
            return
        severe = self._brake_peak >= SEVERE_BRAKE
        ev = {
            "location": label_for_position(self.track_name, self._brake_start_pos),
            "peak_brake_temp": self._brake_temp_peak,
            "duration": self._brake_dur,
            "severe": severe,
            "lockup": self._brake_lockup,
            "hottest_tyre_idx": self._brake_hot_tyre,
        }
        detail = (f"peak_brake={self._brake_peak:.2f} "
                  f"lockup={self._brake_lockup} "
                  f"hot_tyre={TYRE_LABELS[self._brake_hot_tyre]}")
        self.db.insert_event(
            self.current_lap_id, "BRAKING", ev["location"],
            self._brake_temp_peak, self._brake_dur, detail,
        )
        self.eng.on_braking_event(self.lap_number, f.current_time_str, ev)

    # -- event: fuel save / coast ----------------------------------------
    def _update_coasting(self, f: TelemetryFrame) -> None:
        coasting_now = (f.gas < 0.02 and f.brake < BRAKE_ON and f.speed_kmh > 60)
        if coasting_now:
            if not self._coasting:
                self._coasting = True
                self._coast_dur = 0.0
                self._coast_start_pos = f.normalized_car_position
            self._coast_dur += self._tick
        elif self._coasting:
            self._commit_coasting(f)

    def _commit_coasting(self, f: TelemetryFrame) -> None:
        self._coasting = False
        if self._coast_dur < COAST_MIN_S:
            return
        loc = sector_label(f.current_sector)
        detail = f"coast_gap_ms={self._coast_dur * 1000:.0f}"
        self.db.insert_event(
            self.current_lap_id, "FUEL_SAVE", loc,
            self._coast_dur * 1000.0, self._coast_dur, detail,
        )
        self.eng.on_fuel_save(self.lap_number, f.current_time_str,
                              {"location": loc, "duration": self._coast_dur})

    # -- lap completion ---------------------------------------------------
    def _check_lap_complete(self, f: TelemetryFrame) -> None:
        if f.completed_laps > self._last_completed_laps:
            self._last_completed_laps = f.completed_laps

            lap_time = f.last_time_ms / 1000.0
            fuel_used = 0.0
            if self.fuel_at_lap_start is not None:
                fuel_used = max(0.0, self.fuel_at_lap_start - f.fuel)

            stint_avg = mean(self.lap_times) if self.lap_times else None
            self.lap_times.append(lap_time)

            laps_remaining = None
            if fuel_used > 0.01:
                laps_remaining = max(0.0, (f.fuel - TANK_RESERVE) / fuel_used)

            # Finalize the lap that just completed (events already attached to it).
            if self.current_lap_id is not None:
                self.db.finalize_lap(self.current_lap_id, lap_time, fuel_used, f.fuel)
            self.eng.on_lap_complete(self.lap_number, lap_time, stint_avg,
                                     fuel_used, laps_remaining)

            # Open the next lap.
            self.lap_number += 1
            self.current_lap_id = self.db.start_lap(self.session_id, self.lap_number)
            self.fuel_at_lap_start = f.fuel

    # -- main loop --------------------------------------------------------
    def run(self, hz: float = 20.0, max_laps: Optional[int] = None) -> None:
        """The 20Hz polling loop. Identical for live ACC and the simulator.

        Each pass: grab a frame, run the event detectors, check for a finished
        lap, then sleep ~1/hz seconds. With the simulator, `source.read()` just
        hands back the next synthetic frame; with live ACC it reads shared
        memory. The loop code below doesn't care which.
        """
        self._tick = 1.0 / hz       # fallback per-frame time step (see below)
        period = 1.0 / hz           # how long to sleep between polls
        last_packet = -1            # used to ignore frames we've already seen
        prev_time_ms: Optional[int] = None
        try:
            while True:
                f = self.source.read()
                # Only log when actually driving (ACC reports OFF/REPLAY/PAUSE
                # too). The simulator is always "live".
                if not f.is_live:
                    time.sleep(period)
                    continue
                # ACC updates shared memory on its own schedule; if the packet
                # id hasn't changed we polled faster than the game refreshed, so
                # skip the stale frame to avoid double-counting.
                if f.packet_id == last_packet:
                    time.sleep(period)
                    continue
                last_packet = f.packet_id

                # Per-frame time delta from the telemetry clock when available
                # (robust to actual loop speed); falls back to the poll period.
                if prev_time_ms is not None:
                    delta = (f.current_time_ms - prev_time_ms) / 1000.0
                    self._tick = delta if 0.0 < delta < 1.0 else (1.0 / hz)
                prev_time_ms = f.current_time_ms

                self.ensure_session(f)
                self._update_braking(f)
                self._update_coasting(f)
                self._check_lap_complete(f)

                if max_laps is not None and self.lap_number >= max_laps:
                    print(f"=== Reached {max_laps} laps — stopping. ===")
                    break

                time.sleep(period)
        except KeyboardInterrupt:
            print("\n=== Stopped by user. ===")
        finally:
            self.report_summary()
            self.source.close()
            self.db.close()

    def report_summary(self) -> None:
        if not self.lap_times:
            return
        avg = mean(self.lap_times)
        sd = pstdev(self.lap_times) if len(self.lap_times) > 1 else 0.0
        # Endurance Management Score: 100 = perfectly consistent. We penalise the
        # *relative* spread (stdev / avg lap time) so the score is comparable
        # across short and long tracks (e.g. Spa vs the Nordschleife).
        cv = sd / avg if avg > 0 else 0.0
        ems = max(0.0, 100.0 - cv * 6000.0)
        print("\n=== STINT SUMMARY ===")
        print(f"   Laps logged   : {len(self.lap_times)}")
        print(f"   Avg lap time  : {fmt_time(avg)}")
        print(f"   Std deviation : {sd:.3f}s")
        print(f"   EMS (consistency) : {ems:.1f}/100")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_source(args):
    """Pick the telemetry backend. Everything downstream is identical either way.

    --sim  -> the synthetic generator in acc_telemetry.Simulator (no game needed).
    default -> the live ACC shared-memory reader (Windows, game running).

    Both return objects with the same read()/close() interface yielding
    `TelemetryFrame`s, so the logger never needs to know which one it got.
    """
    if args.sim:
        from acc_telemetry import Simulator
        return Simulator(track=args.track, car_model=args.car, seed=args.seed)
    from acc_telemetry import SharedMemoryReader
    try:
        return SharedMemoryReader()
    except Exception as exc:  # pragma: no cover - depends on OS/game
        # Most common cause: ACC isn't running, or we're not on Windows.
        print(f"ERROR: could not open ACC shared memory ({exc}).", file=sys.stderr)
        print("Is ACC running? On non-Windows machines use --sim.", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    p = argparse.ArgumentParser(description="ACC Telemetry & Virtual Race Engineer logger")
    p.add_argument("--sim", action="store_true", help="use the built-in simulator")
    p.add_argument("--db", default="telemetry.db", help="SQLite database path")
    p.add_argument("--hz", type=float, default=20.0, help="polling frequency (default 20Hz)")
    p.add_argument("--laps", type=int, default=None, help="stop after N laps (simulator/testing)")
    p.add_argument("--track", default="spa", help="simulator track name")
    p.add_argument("--car", default="ferrari_296_gt3", help="simulator car model")
    p.add_argument("--seed", type=int, default=None, help="simulator RNG seed")
    args = p.parse_args()

    source = build_source(args)
    db = Database(args.db)
    logger = TelemetryLogger(source, db, RaceEngineer())
    logger.run(hz=args.hz, max_laps=args.laps)


if __name__ == "__main__":
    main()
