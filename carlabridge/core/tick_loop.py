"""30 Hz tick loop running on a dedicated thread (sim domain).

Owns the ONLY thread allowed to call `world.tick()`. Sensor callbacks fire on
CARLA's internal threads (separate); they post raw frames into queues handled
by the async domain.

Structure of each iteration (see design §5):

    1. drain command_queue → scenario.on_command(cmd)           [M6 wires this]
    2. scenario.on_tick_pre(sim_time)
    3. world.tick()
    4. clock.advance()
    5. (M2 will: build snapshot here)
    6. scenario.on_tick_post(sim_time)
    7. sample tick_fps to metrics (every ~1 s)
    8. sleep_until(t0 + delta)  — pacing to real-time 30 Hz
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
import time
from typing import TYPE_CHECKING, Protocol

from carlabridge.commands.enum import RejectCommand
from carlabridge.core.atomic import AtomicRef
from carlabridge.core.clock import SimClock
from carlabridge.core.fleet import Fleet
from carlabridge.core.snapshot import SnapshotBuilder, WorldSnapshot
from carlabridge.obs.event_log import EventLog
from carlabridge.obs.metrics import Metrics

if TYPE_CHECKING:  # pragma: no cover
    from carlabridge.commands.bus import CommandBus
    from carlabridge.sensors.camera import CameraManager

log = logging.getLogger(__name__)


@contextlib.contextmanager
def _high_resolution_timer():
    """Enable Windows' 1 ms multimedia timer for the lifetime of the loop.

    Without this, threading.Event.wait() rounds up to the system tick
    (~15 ms on Windows 10), which makes 30 Hz pacing unreachable.
    No-op on non-Windows platforms.
    """
    if sys.platform != "win32":
        yield
        return
    import ctypes

    winmm = ctypes.windll.winmm
    period_ms = 1
    rc = winmm.timeBeginPeriod(period_ms)
    if rc != 0:  # TIMERR_NOERROR == 0
        log.warning("timeBeginPeriod(%d) returned %d", period_ms, rc)
    try:
        yield
    finally:
        winmm.timeEndPeriod(period_ms)


class WorldLike(Protocol):
    """Minimum surface tick_loop needs. Real `core.world.World` matches it,
    and tests substitute a `FakeWorld` with the same signatures."""

    def tick(self) -> int: ...


class Scenario(Protocol):
    """Sim-domain hooks. All methods called from the tick thread.

    `setup` / `teardown` are NOT called by TickLoop — the lifecycle owner
    (`ScenarioRunner` driven from main) runs them on the main thread before
    and after the tick thread, so they can do CARLA RPCs (spawn_actor, etc.)
    without competing with `world.tick()`.
    """

    name: str

    def on_tick_pre(self, sim_time: float) -> None: ...
    def on_tick_post(self, sim_time: float) -> None: ...
    def on_command(self, cmd: object) -> None: ...


class NoopScenario:
    """No-op fallback (used for --no-carla mode or when no scenario picked)."""

    name = "noop"

    def on_tick_pre(self, sim_time: float) -> None:
        pass

    def on_tick_post(self, sim_time: float) -> None:
        pass

    def on_command(self, cmd: object) -> None:
        pass


class TickLoop:
    """Drives CARLA in sync mode at a fixed real-time pace.

    Run via `start()` which spawns a daemon=False thread. Call `stop()` and
    `join()` from the main thread to shut down cleanly.
    """

    def __init__(
        self,
        *,
        world: WorldLike,
        clock: SimClock,
        fleet: Fleet,
        scenario: Scenario,
        metrics: Metrics,
        event_log: EventLog,
        snapshot_builder: SnapshotBuilder | None = None,
        snapshot_ref: AtomicRef[WorldSnapshot] | None = None,
        camera_manager: "CameraManager | None" = None,
        command_bus: "CommandBus | None" = None,
        behind_warn_threshold: float = 1.5,
    ) -> None:
        self._world = world
        self._clock = clock
        self._fleet = fleet
        self._scenario = scenario
        self._metrics = metrics
        self._event_log = event_log
        self._snapshot_builder = snapshot_builder
        self._snapshot_ref = snapshot_ref
        self._camera_manager = camera_manager
        self._command_bus = command_bus
        self._behind_warn_threshold = behind_warn_threshold

        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._behind_streak = 0
        # FPS sampling state
        self._fps_window_start = 0.0
        self._fps_window_count = 0

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("TickLoop already started")
        self._clock.start()
        self._fps_window_start = time.monotonic()
        self._fps_window_count = 0
        self._thread = threading.Thread(
            target=self._run, name="carlabridge-tick", daemon=False
        )
        self._thread.start()
        log.info("tick loop started (delta=%.4fs)", self._clock.delta)

    def stop(self) -> None:
        self._shutdown.set()

    def join(self, timeout: float | None = 3.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning("tick thread did not exit within %.1fs", timeout or 0)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- the loop ------------------------------------------------------

    def _run(self) -> None:
        delta = self._clock.delta
        log.info("entering tick loop @ %.1f Hz", 1.0 / delta)
        with _high_resolution_timer():
            try:
                while not self._shutdown.is_set():
                    t0 = time.perf_counter()
                    try:
                        self._one_iteration()
                    except Exception:
                        log.exception("tick iteration failed; stopping loop")
                        self._event_log.add(
                            "danger", "BRIDGE", "tick iteration raised; loop stopping"
                        )
                        break
                    self._pace_to_next_tick(t0, delta)
            finally:
                log.info("tick loop exited (sim_time=%.2fs ticks=%d)",
                         self._clock.sim_time, self._clock.tick_count)

    def _one_iteration(self) -> None:
        # 1. drain commands → scenario.on_command, ack/reject through CommandBus
        if self._command_bus is not None:
            for cmd in self._command_bus.drain():
                try:
                    self._scenario.on_command(cmd)
                except RejectCommand as r:
                    self._command_bus.reject(
                        cmd.id, target=cmd.target, reason=str(r)
                    )
                except Exception as e:
                    log.exception("scenario.on_command raised")
                    self._command_bus.reject(
                        cmd.id,
                        target=cmd.target,
                        reason=f"scenario error: {type(e).__name__}: {e}",
                    )
                else:
                    self._command_bus.ack(cmd.id, target=cmd.target)
        # 2. pre-tick scenario hook
        self._scenario.on_tick_pre(self._clock.sim_time)
        # 3. advance CARLA one step
        self._world.tick()
        # 4. advance bridge clock
        self._clock.advance()
        # 5. build & publish WorldSnapshot (M2)
        if self._snapshot_builder is not None and self._snapshot_ref is not None:
            try:
                snap = self._snapshot_builder.build(self._fleet, self._clock.sim_time)
                self._snapshot_ref.set(snap)
            except Exception:
                log.exception("snapshot build failed; tick continues")
        # 6. follows_virtual cameras: drive set_transform from VirtualMember pose (M4)
        if self._camera_manager is not None:
            try:
                self._camera_manager.update_followers(self._fleet)
            except Exception:
                log.exception("camera update_followers failed; tick continues")
        # 7. post-tick scenario hook
        self._scenario.on_tick_post(self._clock.sim_time)
        # 8. fps sampling
        self._sample_fps()

    def _sample_fps(self) -> None:
        self._fps_window_count += 1
        now = time.monotonic()
        elapsed = now - self._fps_window_start
        if elapsed >= 1.0:
            fps = self._fps_window_count / elapsed
            self._metrics.set("tick_fps", round(fps, 2))
            self._metrics.set("tick_count_total", self._clock.tick_count)
            self._fps_window_start = now
            self._fps_window_count = 0

    def _pace_to_next_tick(self, cycle_start: float, delta: float) -> None:
        elapsed = time.perf_counter() - cycle_start
        slack = delta - elapsed
        if slack > 0:
            # Interruptible wait — shutdown breaks it immediately.
            self._shutdown.wait(slack)
            self._behind_streak = 0
        else:
            self._behind_streak += 1
            # Sustained overrun for ~1s @ 30Hz = 30 frames behind threshold.
            if elapsed > delta * self._behind_warn_threshold and (
                self._behind_streak == 1 or self._behind_streak % 30 == 0
            ):
                log.warning(
                    "tick behind schedule: cycle=%.1fms target=%.1fms (streak=%d)",
                    elapsed * 1000,
                    delta * 1000,
                    self._behind_streak,
                )
                self._event_log.add(
                    "warn",
                    "BRIDGE",
                    f"tick behind: {elapsed * 1000:.0f}ms vs target {delta * 1000:.0f}ms",
                )
