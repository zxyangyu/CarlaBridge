"""ScenarioRunner.run_in_sim_domain primitive (R6-01)."""

from __future__ import annotations

import asyncio
import threading

import pytest

from carlabridge.core.fleet import Fleet
from carlabridge.obs.event_log import EventLog
from carlabridge.scenarios.base import Scenario
from carlabridge.scenarios.runner import ScenarioRunner
from carlabridge.sensors.camera import CameraManager


class _NoopScenario(Scenario):
    """Empty scenario — setup/teardown are no-ops so the runner can start
    without a CARLA world."""

    name = "noop"

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass


def _make_runner() -> ScenarioRunner:
    return ScenarioRunner(
        _NoopScenario,
        world=None,  # never accessed by _NoopScenario.setup
        fleet=Fleet(),
        camera_manager=CameraManager(),
        event_log=EventLog(capacity=50),
    )


# ---- happy path ----------------------------------------------------------


async def test_run_in_sim_domain_resolves_with_return_value():
    runner = _make_runner()
    future = runner.run_in_sim_domain(lambda: 42)
    # Simulate the tick thread.
    drain_thread = threading.Thread(target=runner.drain_sim_tasks)
    drain_thread.start()
    drain_thread.join()
    assert await asyncio.wait_for(future, timeout=2.0) == 42


async def test_run_in_sim_domain_passes_args_and_kwargs():
    runner = _make_runner()

    def add(a: int, b: int, *, scale: int = 1) -> int:
        return (a + b) * scale

    future = runner.run_in_sim_domain(add, 3, 4, scale=10)
    threading.Thread(target=runner.drain_sim_tasks).start()
    assert await asyncio.wait_for(future, timeout=2.0) == 70


async def test_run_in_sim_domain_resolves_dict_result():
    runner = _make_runner()
    future = runner.run_in_sim_domain(lambda: {"new_run_id": 7, "cancelled": []})
    threading.Thread(target=runner.drain_sim_tasks).start()
    result = await asyncio.wait_for(future, timeout=2.0)
    assert result == {"new_run_id": 7, "cancelled": []}


# ---- error path ----------------------------------------------------------


async def test_run_in_sim_domain_propagates_exception_via_set_exception():
    runner = _make_runner()

    def boom() -> None:
        raise ValueError("kaboom")

    future = runner.run_in_sim_domain(boom)
    threading.Thread(target=runner.drain_sim_tasks).start()
    with pytest.raises(ValueError, match="kaboom"):
        await asyncio.wait_for(future, timeout=2.0)


async def test_run_in_sim_domain_propagates_runtime_error():
    runner = _make_runner()

    def boom() -> None:
        raise RuntimeError("scenario_resetting")

    future = runner.run_in_sim_domain(boom)
    threading.Thread(target=runner.drain_sim_tasks).start()
    with pytest.raises(RuntimeError, match="scenario_resetting"):
        await asyncio.wait_for(future, timeout=2.0)


# ---- ordering ------------------------------------------------------------


async def test_run_in_sim_domain_preserves_fifo_order():
    runner = _make_runner()
    futures = [runner.run_in_sim_domain(lambda i=i: i) for i in range(5)]
    threading.Thread(target=runner.drain_sim_tasks).start()
    results = await asyncio.gather(*futures)
    assert results == [0, 1, 2, 3, 4]


async def test_drain_is_noop_on_empty_queue():
    runner = _make_runner()
    # Must not raise.
    runner.drain_sim_tasks()


async def test_is_resetting_reads_through_to_scenario():
    runner = _make_runner()
    assert runner.is_resetting() is False  # no scenario yet
    runner.start()
    assert runner.is_resetting() is False
    runner._scenario._resetting = True  # type: ignore[attr-defined]
    assert runner.is_resetting() is True
    runner.stop()
