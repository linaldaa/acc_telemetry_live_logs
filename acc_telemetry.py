"""
acc_telemetry.py
================
Telemetry source layer for the ACC Telemetry & Virtual Race Engineer system.

Provides two interchangeable backends that both yield a unified `TelemetryFrame`:

  1. SharedMemoryReader  - reads ACC's live Windows shared memory (real telemetry).
  2. Simulator           - generates realistic fake telemetry so the whole
                           pipeline (logger, DB, dashboard) runs without ACC.

The logger is backend-agnostic: it only ever sees `TelemetryFrame` objects, so
the same event-detection and live-feed code works for real and simulated data.

ACC shared-memory layout reference: the three pages are mapped at
  Local\\acpmf_physics, Local\\acpmf_graphics, Local\\acpmf_static
"""

from __future__ import annotations

import ctypes
import math
import random
from dataclasses import dataclass, field
from typing import List, Optional

# ACC tyre / corner order is Front-Left, Front-Right, Rear-Left, Rear-Right
TYRE_LABELS = ["FL", "FR", "RL", "RR"]

# AC_STATUS enum
AC_OFF = 0
AC_REPLAY = 1
AC_LIVE = 2
AC_PAUSE = 3


# ---------------------------------------------------------------------------
# Unified frame consumed by the logger
# ---------------------------------------------------------------------------
@dataclass
class TelemetryFrame:
    """A single 50ms snapshot of everything the logger cares about."""

    packet_id: int = 0
    status: int = AC_LIVE

    # Driver inputs / engine
    gas: float = 0.0
    brake: float = 0.0
    steer_angle: float = 0.0   # normalized: 0 = centred, +/- = right/left
    gear: int = 0
    rpm: int = 0
    speed_kmh: float = 0.0
    fuel: float = 0.0

    # Per-corner physics (FL, FR, RL, RR)
    wheel_slip: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    tyre_temp: List[float] = field(default_factory=lambda: [80.0, 80.0, 80.0, 80.0])
    brake_temp: List[float] = field(default_factory=lambda: [200.0, 200.0, 200.0, 200.0])
    brake_bias: float = 0.50  # fraction towards front

    # Lap / graphics state
    completed_laps: int = 0
    current_time_ms: int = 0
    last_time_ms: int = 0
    current_time_str: str = "00:00.000"
    normalized_car_position: float = 0.0  # 0..1 around the lap
    current_sector: int = 0
    in_pit: bool = False

    # Session statics
    track: str = "Unknown"
    car_model: str = "Unknown"
    max_fuel: float = 120.0

    @property
    def is_live(self) -> bool:
        return self.status == AC_LIVE


# ---------------------------------------------------------------------------
# ACC shared-memory C structures (Windows)
# ---------------------------------------------------------------------------
class SPageFilePhysics(ctypes.Structure):
    _fields_ = [
        ("packetId", ctypes.c_int),
        ("gas", ctypes.c_float),
        ("brake", ctypes.c_float),
        ("fuel", ctypes.c_float),
        ("gear", ctypes.c_int),
        ("rpms", ctypes.c_int),
        ("steerAngle", ctypes.c_float),
        ("speedKmh", ctypes.c_float),
        ("velocity", ctypes.c_float * 3),
        ("accG", ctypes.c_float * 3),
        ("wheelSlip", ctypes.c_float * 4),
        ("wheelLoad", ctypes.c_float * 4),
        ("wheelsPressure", ctypes.c_float * 4),
        ("wheelAngularSpeed", ctypes.c_float * 4),
        ("tyreWear", ctypes.c_float * 4),
        ("tyreDirtyLevel", ctypes.c_float * 4),
        ("tyreCoreTemperature", ctypes.c_float * 4),
        ("camberRAD", ctypes.c_float * 4),
        ("suspensionTravel", ctypes.c_float * 4),
        ("drs", ctypes.c_float),
        ("tc", ctypes.c_float),
        ("heading", ctypes.c_float),
        ("pitch", ctypes.c_float),
        ("roll", ctypes.c_float),
        ("cgHeight", ctypes.c_float),
        ("carDamage", ctypes.c_float * 5),
        ("numberOfTyresOut", ctypes.c_int),
        ("pitLimiterOn", ctypes.c_int),
        ("abs", ctypes.c_float),
        ("kersCharge", ctypes.c_float),
        ("kersInput", ctypes.c_float),
        ("autoShifterOn", ctypes.c_int),
        ("rideHeight", ctypes.c_float * 2),
        ("turboBoost", ctypes.c_float),
        ("ballast", ctypes.c_float),
        ("airDensity", ctypes.c_float),
        ("airTemp", ctypes.c_float),
        ("roadTemp", ctypes.c_float),
        ("localAngularVel", ctypes.c_float * 3),
        ("finalFF", ctypes.c_float),
        ("performanceMeter", ctypes.c_float),
        ("engineBrake", ctypes.c_int),
        ("ersRecoveryLevel", ctypes.c_int),
        ("ersPowerLevel", ctypes.c_int),
        ("ersHeatCharging", ctypes.c_int),
        ("ersIsCharging", ctypes.c_int),
        ("kersCurrentKJ", ctypes.c_float),
        ("drsAvailable", ctypes.c_int),
        ("drsEnabled", ctypes.c_int),
        ("brakeTemp", ctypes.c_float * 4),
        ("clutch", ctypes.c_float),
        ("tyreTempI", ctypes.c_float * 4),
        ("tyreTempM", ctypes.c_float * 4),
        ("tyreTempO", ctypes.c_float * 4),
        ("isAIControlled", ctypes.c_int),
        ("tyreContactPoint", (ctypes.c_float * 3) * 4),
        ("tyreContactNormal", (ctypes.c_float * 3) * 4),
        ("tyreContactHeading", (ctypes.c_float * 3) * 4),
        ("brakeBias", ctypes.c_float),
        ("localVelocity", ctypes.c_float * 3),
    ]


class SPageFileGraphic(ctypes.Structure):
    _fields_ = [
        ("packetId", ctypes.c_int),
        ("status", ctypes.c_int),
        ("session", ctypes.c_int),
        ("currentTime", ctypes.c_wchar * 15),
        ("lastTime", ctypes.c_wchar * 15),
        ("bestTime", ctypes.c_wchar * 15),
        ("split", ctypes.c_wchar * 15),
        ("completedLaps", ctypes.c_int),
        ("position", ctypes.c_int),
        ("iCurrentTime", ctypes.c_int),
        ("iLastTime", ctypes.c_int),
        ("iBestTime", ctypes.c_int),
        ("sessionTimeLeft", ctypes.c_float),
        ("distanceTraveled", ctypes.c_float),
        ("isInPit", ctypes.c_int),
        ("currentSectorIndex", ctypes.c_int),
        ("lastSectorTime", ctypes.c_int),
        ("numberOfLaps", ctypes.c_int),
        ("tyreCompound", ctypes.c_wchar * 33),
        ("replayTimeMultiplier", ctypes.c_float),
        ("normalizedCarPosition", ctypes.c_float),
    ]


class SPageFileStatic(ctypes.Structure):
    _fields_ = [
        ("smVersion", ctypes.c_wchar * 15),
        ("acVersion", ctypes.c_wchar * 15),
        ("numberOfSessions", ctypes.c_int),
        ("numCars", ctypes.c_int),
        ("carModel", ctypes.c_wchar * 33),
        ("track", ctypes.c_wchar * 33),
        ("playerName", ctypes.c_wchar * 33),
        ("playerSurname", ctypes.c_wchar * 33),
        ("playerNick", ctypes.c_wchar * 33),
        ("sectorCount", ctypes.c_int),
        ("maxTorque", ctypes.c_float),
        ("maxPower", ctypes.c_float),
        ("maxRpm", ctypes.c_int),
        ("maxFuel", ctypes.c_float),
    ]


def _ms_to_str(ms: int) -> str:
    if ms <= 0 or ms >= 999999999:
        return "00:00.000"
    minutes = ms // 60000
    seconds = (ms % 60000) / 1000.0
    return f"{minutes:02d}:{seconds:06.3f}"


# ---------------------------------------------------------------------------
# Real backend: ACC shared memory (Windows only)
# ---------------------------------------------------------------------------
class SharedMemoryReader:
    """Reads live telemetry from ACC's shared memory. Windows only."""

    def __init__(self) -> None:
        import mmap  # local import keeps the module importable off-Windows

        self._physics = mmap.mmap(-1, ctypes.sizeof(SPageFilePhysics), "Local\\acpmf_physics")
        self._graphics = mmap.mmap(-1, ctypes.sizeof(SPageFileGraphic), "Local\\acpmf_graphics")
        self._static = mmap.mmap(-1, ctypes.sizeof(SPageFileStatic), "Local\\acpmf_static")

    def read(self) -> TelemetryFrame:
        self._physics.seek(0)
        self._graphics.seek(0)
        self._static.seek(0)
        phys = SPageFilePhysics.from_buffer_copy(self._physics.read(ctypes.sizeof(SPageFilePhysics)))
        gfx = SPageFileGraphic.from_buffer_copy(self._graphics.read(ctypes.sizeof(SPageFileGraphic)))
        stat = SPageFileStatic.from_buffer_copy(self._static.read(ctypes.sizeof(SPageFileStatic)))

        return TelemetryFrame(
            packet_id=gfx.packetId,
            status=gfx.status,
            gas=phys.gas,
            brake=phys.brake,
            steer_angle=phys.steerAngle,
            gear=phys.gear,
            rpm=phys.rpms,
            speed_kmh=phys.speedKmh,
            fuel=phys.fuel,
            wheel_slip=list(phys.wheelSlip),
            tyre_temp=list(phys.tyreCoreTemperature),
            brake_temp=list(phys.brakeTemp),
            brake_bias=phys.brakeBias,
            completed_laps=gfx.completedLaps,
            current_time_ms=gfx.iCurrentTime,
            last_time_ms=gfx.iLastTime,
            current_time_str=gfx.currentTime or _ms_to_str(gfx.iCurrentTime),
            normalized_car_position=gfx.normalizedCarPosition,
            current_sector=gfx.currentSectorIndex,
            in_pit=bool(gfx.isInPit),
            track=stat.track or "Unknown",
            car_model=stat.carModel or "Unknown",
            max_fuel=stat.maxFuel or 120.0,
        )

    def close(self) -> None:
        for m in (self._physics, self._graphics, self._static):
            try:
                m.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Simulator backend
# ---------------------------------------------------------------------------
# WHY THIS EXISTS:
# ACC's shared memory only exists on Windows while the game is running. The
# Simulator lets you run the ENTIRE pipeline (logger -> events -> SQLite ->
# dashboard) with no game at all, so you can develop and demo offline. It
# produces the same `TelemetryFrame` objects the real reader does, so the
# logger cannot tell the two apart.
#
# A track profile is just a list of corners positioned around the lap, plus a
# representative GT3 lap time. Each corner entry defines:
#   pos       - where on the lap it sits, as a fraction from 0.0 (start) to 1.0
#   name      - label used in the live feed / dashboard
#   brake     - peak brake pressure (0..1) applied at the corner
#   gear      - gear taken through the corner
#   heavy     - whether it's a hard stop (drives how hot the brakes get)
#   lockup_p  - probability per lap that the front-left tyre locks up here
#
# TRACK_PROFILES is keyed by track name. The simulator uses a profile to decide
# where to brake; the logger uses the SAME profile (via label_for_position) to
# name the corner an event happened at, so sim and live ACC stay consistent.

_SPA_CORNERS = [
    {"pos": 0.08, "name": "La Source", "brake": 1.00, "gear": 2, "heavy": True, "lockup_p": 0.45},
    {"pos": 0.22, "name": "Les Combes", "brake": 0.85, "gear": 3, "heavy": True, "lockup_p": 0.20},
    {"pos": 0.37, "name": "Rivage", "brake": 0.70, "gear": 2, "heavy": False, "lockup_p": 0.10},
    {"pos": 0.55, "name": "Pouhon", "brake": 0.40, "gear": 4, "heavy": False, "lockup_p": 0.02},
    {"pos": 0.70, "name": "Fagnes", "brake": 0.75, "gear": 3, "heavy": False, "lockup_p": 0.10},
    {"pos": 0.83, "name": "Bus Stop Chicane", "brake": 0.95, "gear": 2, "heavy": True, "lockup_p": 0.30},
]

# Nordschleife: ~8 minute GT3 lap with many more braking zones. Positions are
# approximate fractions of the lap; names are the famous landmark corners.
_NORDSCHLEIFE_CORNERS = [
    {"pos": 0.05, "name": "Hatzenbach", "brake": 0.70, "gear": 3, "heavy": False, "lockup_p": 0.20},
    {"pos": 0.13, "name": "Aremberg", "brake": 0.85, "gear": 2, "heavy": True, "lockup_p": 0.25},
    {"pos": 0.22, "name": "Adenauer Forst", "brake": 0.95, "gear": 2, "heavy": True, "lockup_p": 0.30},
    {"pos": 0.31, "name": "Bergwerk", "brake": 1.00, "gear": 2, "heavy": True, "lockup_p": 0.40},
    {"pos": 0.42, "name": "Karussell", "brake": 0.80, "gear": 2, "heavy": True, "lockup_p": 0.20},
    {"pos": 0.55, "name": "Brünnchen", "brake": 0.75, "gear": 3, "heavy": False, "lockup_p": 0.15},
    {"pos": 0.66, "name": "Pflanzgarten", "brake": 0.70, "gear": 3, "heavy": False, "lockup_p": 0.15},
    {"pos": 0.74, "name": "Schwalbenschwanz", "brake": 0.90, "gear": 2, "heavy": True, "lockup_p": 0.25},
    {"pos": 0.82, "name": "Galgenkopf", "brake": 0.55, "gear": 4, "heavy": False, "lockup_p": 0.05},
    {"pos": 0.97, "name": "Tiergarten", "brake": 0.85, "gear": 2, "heavy": True, "lockup_p": 0.20},
]

TRACK_PROFILES = {
    # name: (base_lap_time_seconds, corners)
    "spa": (138.0, _SPA_CORNERS),                 # ~2:18
    "nurburgring_24h": (480.0, _NORDSCHLEIFE_CORNERS),
    "nordschleife": (480.0, _NORDSCHLEIFE_CORNERS),  # ~8:00
}

# Default profile when an unknown track name is given.
_DEFAULT_PROFILE = ("spa", TRACK_PROFILES["spa"])


def get_profile(track: str):
    """Return (base_lap_time, corners) for a track name (falls back to Spa)."""
    key = (track or "").strip().lower()
    if key in TRACK_PROFILES:
        return TRACK_PROFILES[key]
    return _DEFAULT_PROFILE[1]


def label_for_position(track: str, pos: float) -> str:
    """Name the nearest corner to a normalized lap position (0..1).

    Used by the logger to label where an event happened. Works for both the
    simulator and live ACC, since both only know the car's lap position.
    """
    _, corners = get_profile(track)
    if not corners:
        return "Unknown"
    # Braking happens just BEFORE a corner, so label with the next corner ahead
    # of this position (wrapping around the start/finish line), not merely the
    # closest one — otherwise a braking zone can get tagged with the corner the
    # car just left.
    forward = min(corners, key=lambda c: (c["pos"] - pos) % 1.0)
    return forward["name"]


class Simulator:
    """Generates realistic telemetry frames advancing 0.05s per read().

    Think of it as a fake driver lapping a fake track. It holds an internal
    clock (`_t`, in seconds) representing how far into the current lap we are.
    Every call to read() emits one frame and then advances that clock by 0.05s
    — i.e. it pretends to be a 20Hz feed (20 frames/sec * 0.05s = 1 real second).
    Crossing the lap duration rolls over to a fresh lap with a slightly
    different lap time, which is what gives the dashboard a realistic spread of
    lap times (and therefore a meaningful consistency / EMS score).
    """

    def __init__(
        self,
        track: str = "spa",
        car_model: str = "ferrari_296_gt3",
        base_lap_time_s: Optional[float] = None,  # None -> use the track profile
        max_fuel: float = 120.0,
        start_fuel: Optional[float] = None,
        seed: Optional[int] = None,
    ) -> None:
        self.track = track
        self.car_model = car_model
        # Pull this track's reference lap time and corner layout from its
        # profile (e.g. ~138s for Spa, ~480s for the Nordschleife). An explicit
        # base_lap_time_s overrides the profile default.
        profile_lap_time, self.corners = get_profile(track)
        self.base_lap_time = base_lap_time_s if base_lap_time_s is not None else profile_lap_time
        self.max_fuel = max_fuel
        # `seed` makes a run fully reproducible: same seed -> identical laps,
        # lockups and lap times. Leave it None for different data every run.
        self.fuel = start_fuel if start_fuel is not None else max_fuel
        self.rng = random.Random(seed)

        self._t = 0.0  # elapsed time within the CURRENT lap, in seconds
        self._lap_duration = self._sample_lap_duration()  # this lap's target time
        self.completed_laps = 0      # mirrors ACC's graphics.completedLaps
        self._last_time_ms = 0       # the just-finished lap time (ms), for the logger
        self._packet = 0             # increments each frame, like ACC's packetId
        # Fuel burn scales with lap length: a longer lap (Nordschleife) burns
        # proportionally more than a short one (Spa). ~0.0207 L per second.
        self._fuel_rate = 0.0207     # litres per second of lap time
        self._fuel_per_lap = self.base_lap_time * self._fuel_rate
        # Decide up front, for this lap, which corners will suffer a lockup.
        self._lockup_this_lap = {}
        self._decide_lockups()

    def _sample_lap_duration(self) -> float:
        # Lap-to-lap variability (scaled to lap length) drives the consistency
        # / EMS score: ~0.55s on Spa, ~1.9s on the longer Nordschleife.
        return self.base_lap_time + self.rng.gauss(0.0, self.base_lap_time * 0.004)

    def _decide_lockups(self) -> None:
        self._lockup_this_lap = {
            i: (self.rng.random() < c["lockup_p"]) for i, c in enumerate(self.corners)
        }

    def _nearest_corner(self, pos: float):
        """Return (corner, intensity 0..1) if we're inside a braking zone, else (None, 0).

        `intensity` ramps smoothly from 0 up to 1 and back to 0 across the
        braking window (a sine bump), so brake pressure, speed and temperatures
        rise and fall naturally through the corner instead of snapping on/off.
        """
        best = None
        best_int = 0.0
        for i, c in enumerate(self.corners):
            # The braking window spans ~6% of the lap leading into the apex.
            d = pos - (c["pos"] - 0.06)
            if 0.0 <= d <= 0.06:
                intensity = math.sin((d / 0.06) * math.pi)  # 0 -> 1 -> 0 bump
                if intensity > best_int:
                    best_int = intensity
                    best = (i, c)
        return best, best_int

    def read(self) -> TelemetryFrame:
        """Produce one telemetry frame for the current instant in the lap."""
        self._packet += 1
        # Where we are around the lap, 0.0 (start/finish line) .. 1.0 (next line).
        pos = self._t / self._lap_duration

        # --- Default "flat out on the straight" state ----------------------
        # Start every frame assuming we're at full throttle in top gear, then
        # override below if we're actually in a braking zone or coasting.
        corner_hit, intensity = self._nearest_corner(pos)
        brake = 0.0
        gas = 0.95 + self.rng.uniform(-0.05, 0.05)
        steer = 0.0 + self.rng.uniform(-0.02, 0.02)         # ~straight on the straight
        gear = 5
        speed = 235.0 + self.rng.uniform(-10, 10)
        slip = [0.05] * 4                                   # FL, FR, RL, RR
        tyre = [92.0 + self.rng.uniform(-3, 3) for _ in range(4)]   # surface °C
        btemp = [380.0 + self.rng.uniform(-30, 30) for _ in range(4)]  # disc °C
        # Sector boundaries at ~1/3 and ~2/3 of the lap (ACC uses 3 sectors).
        sector = 0 if pos < 0.34 else (1 if pos < 0.67 else 2)

        if corner_hit is not None:
            # --- We're braking into a corner -------------------------------
            idx, c = corner_hit
            brake = c["brake"] * intensity      # pressure follows the sine bump
            gas = max(0.0, 0.15 - intensity * 0.15)
            gear = c["gear"]
            speed = max(70.0, 235.0 - intensity * 150.0)   # scrub off speed
            # Steering winds on through the corner. Alternate L/R by corner so a
            # stint shows both lock directions. Because steer rises while brake
            # is still applied, the data exhibits realistic trail-braking.
            direction = 1.0 if idx % 2 == 0 else -1.0
            steer = direction * intensity * (0.6 + self.rng.uniform(-0.05, 0.05))
            # Brakes and tyres heat up in proportion to how hard we're braking;
            # "heavy" corners dump more energy into the discs.
            heat = intensity * (260.0 if c["heavy"] else 150.0)
            btemp = [b + heat + self.rng.uniform(-20, 20) for b in btemp]
            tyre = [t + intensity * 14.0 for t in tyre]
            # Lockup (only near the corner's peak braking): the front-left wheel
            # stops turning, so its slip ratio and surface temp spike. This is
            # exactly the pattern the logger flags as a lockup + bias advice.
            if self._lockup_this_lap.get(idx) and intensity > 0.6:
                slip[0] = 1.6 + self.rng.uniform(0, 0.6)   # FL well above 1.0
                tyre[0] += 18.0
                btemp[0] += 120.0
        else:
            # --- Lift-and-coast (fuel saving) ------------------------------
            # In a stretch before the chicane, sometimes lift fully off both
            # pedals. The logger picks this up as a "Fuel Save" event.
            if 0.62 < pos < 0.68 and self.rng.random() < 0.5:
                gas = 0.0
                brake = 0.0
                speed = 215.0

        # Rough engine RPM as a function of gear, just so the field looks real.
        rpm = int(3500 + (gear / 6.0) * 4500 + self.rng.uniform(-200, 200))

        frame = TelemetryFrame(
            packet_id=self._packet,
            status=AC_LIVE,
            gas=round(max(0.0, min(1.0, gas)), 3),
            brake=round(max(0.0, min(1.0, brake)), 3),
            steer_angle=round(max(-1.0, min(1.0, steer)), 3),
            gear=gear,
            rpm=rpm,
            speed_kmh=round(speed, 1),
            fuel=round(self.fuel, 3),
            wheel_slip=[round(s, 3) for s in slip],
            tyre_temp=[round(t, 1) for t in tyre],
            brake_temp=[round(b, 1) for b in btemp],
            brake_bias=0.54,
            completed_laps=self.completed_laps,
            current_time_ms=int(self._t * 1000),
            last_time_ms=self._last_time_ms,
            current_time_str=_ms_to_str(int(self._t * 1000)),
            normalized_car_position=round(pos, 4),
            current_sector=sector,
            in_pit=False,
            track=self.track,
            car_model=self.car_model,
            max_fuel=self.max_fuel,
        )

        # --- Advance the clock by one 20Hz tick AFTER building the frame ---
        self._t += 0.05
        # Burn fuel proportionally to the slice of the lap we just covered.
        self.fuel = max(0.0, self.fuel - (self._fuel_per_lap / self._lap_duration) * 0.05)
        # Lap rollover: record the finished lap time, bump the counter, reset
        # the clock, and re-roll this next lap's time, fuel rate and lockups —
        # the lap-to-lap variation is what makes the EMS/consistency score real.
        if self._t >= self._lap_duration:
            self._last_time_ms = int(self._lap_duration * 1000)
            self.completed_laps += 1
            self._t = 0.0
            self._lap_duration = self._sample_lap_duration()
            # Re-jitter fuel burn a little each lap, still scaled to lap length.
            self._fuel_per_lap = self.base_lap_time * self._fuel_rate * (1 + self.rng.uniform(-0.05, 0.05))
            self._decide_lockups()

        return frame

    def close(self) -> None:
        pass
