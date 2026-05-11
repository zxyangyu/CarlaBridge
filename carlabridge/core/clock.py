"""Simulation clock: sim_time advances by fixed delta each tick.

Independent of CARLA's internal frame counter; the bridge owns this clock and
resets it when a scenario starts so script timing is from 0.
"""

from __future__ import annotations

import time
from threading import RLock


class SimClock:
    """Thread-safe sim/wall clock pair.

    `sim_time` advances by `delta` each `advance()` call (driven by tick loop).
    `wall_elapsed` is monotonic seconds since `start()` (or last `reset()`).
    """

    def __init__(self, delta: float) -> None:
        if delta <= 0:
            raise ValueError("delta must be positive")
        self._delta = float(delta)
        self._sim_time = 0.0
        self._tick_count = 0
        self._wall_start = time.monotonic()
        self._lock = RLock()

    @property
    def delta(self) -> float:
        return self._delta

    @property
    def sim_time(self) -> float:
        with self._lock:
            return self._sim_time

    @property
    def tick_count(self) -> int:
        with self._lock:
            return self._tick_count

    @property
    def wall_elapsed(self) -> float:
        return time.monotonic() - self._wall_start

    def start(self) -> None:
        """Reset both clocks to zero. Call once when scenario starts."""
        with self._lock:
            self._sim_time = 0.0
            self._tick_count = 0
            self._wall_start = time.monotonic()

    def advance(self) -> float:
        """Advance sim_time by one delta. Returns new sim_time."""
        with self._lock:
            self._sim_time += self._delta
            self._tick_count += 1
            return self._sim_time
