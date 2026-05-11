"""Unit tests for TickLoop using a FakeWorld stub (no CARLA needed)."""

from __future__ import annotations

import threading
import time

from carlabridge.core.clock import SimClock
from carlabridge.core.fleet import Fleet
from carlabridge.core.tick_loop import NoopScenario, TickLoop
from carlabridge.obs.event_log import EventLog
from carlabridge.obs.metrics import Metrics


class FakeWorld:
    """Minimal stand-in for core.world.World — tick() returns instantly."""

    def __init__(self, tick_cost_s: float = 0.0):
        self.tick_cost_s = tick_cost_s
        self.tick_calls = 0
        self._lock = threading.Lock()

    def tick(self) -> int:
        if self.tick_cost_s > 0:
            time.sleep(self.tick_cost_s)
        with self._lock:
            self.tick_calls += 1
            return self.tick_calls


class RecordingScenario:
    name = "recording"

    def __init__(self):
        self.pre_calls = 0
        self.post_calls = 0
        self.setup_called = False
        self.teardown_called = False

    def setup(self, world, fleet):
        self.setup_called = True

    def on_tick_pre(self, sim_time):
        self.pre_calls += 1

    def on_tick_post(self, sim_time):
        self.post_calls += 1

    def on_command(self, cmd):
        pass

    def teardown(self):
        self.teardown_called = True


def _make_loop(tick_cost: float = 0.0, scenario=None, delta: float = 0.0333):
    return TickLoop(
        world=FakeWorld(tick_cost_s=tick_cost),
        clock=SimClock(delta=delta),
        fleet=Fleet(),
        scenario=scenario or NoopScenario(),
        metrics=Metrics(),
        event_log=EventLog(capacity=100),
    )


def test_tick_loop_paces_to_target_hz():
    """Run for ~1 second at 30 Hz, expect 28–32 ticks (allow some slack)."""
    loop = _make_loop(tick_cost=0.001, delta=1 / 30)
    loop.start()
    time.sleep(1.05)
    loop.stop()
    loop.join()
    metrics = loop._metrics.snapshot()
    fps = metrics.get("tick_fps", 0)
    assert 27 <= fps <= 33, f"expected ~30 fps, got {fps}"


def test_tick_loop_calls_scenario_hooks():
    scen = RecordingScenario()
    loop = _make_loop(tick_cost=0.001, scenario=scen, delta=1 / 60)
    loop.start()
    time.sleep(0.3)
    loop.stop()
    loop.join()
    assert scen.setup_called
    assert scen.teardown_called
    assert scen.pre_calls > 5
    assert scen.pre_calls == scen.post_calls


def test_tick_loop_stop_is_fast():
    loop = _make_loop(tick_cost=0.001, delta=1 / 30)
    loop.start()
    time.sleep(0.1)
    t0 = time.perf_counter()
    loop.stop()
    loop.join()
    elapsed = time.perf_counter() - t0
    # shutdown.wait() inside _pace_to_next_tick should interrupt immediately.
    assert elapsed < 0.2, f"slow shutdown: {elapsed:.3f}s"


def test_tick_loop_double_start_raises():
    loop = _make_loop()
    loop.start()
    try:
        import pytest
        with pytest.raises(RuntimeError):
            loop.start()
    finally:
        loop.stop()
        loop.join()


def test_tick_loop_advances_sim_time():
    loop = _make_loop(tick_cost=0.0, delta=1 / 60)
    loop.start()
    time.sleep(0.25)
    loop.stop()
    loop.join()
    # sim_time should have advanced ~0.25s of sim (15 ticks @ 60Hz).
    assert loop._clock.sim_time > 0.1
    assert loop._clock.tick_count > 5
