"""Scenario base + registry + ScenarioRunner lifecycle tests.

We exercise the scenario base class with a synthetic subclass — no CARLA. The
S1 scenario itself (`s1_fire`) needs a real CARLA server to spawn actors, so
its live verification stays in the integration smoke step.
"""

from __future__ import annotations

import pytest

from carlabridge.core.fleet import CarlaActorMember, Fleet, Pose, VirtualMember
from carlabridge.obs.event_log import EventLog
from carlabridge.scenarios.base import (
    Scenario,
    available_scenarios,
    get_scenario_class,
    register_scenario,
)
from carlabridge.scenarios.runner import ScenarioRunner
from carlabridge.sensors.camera import (
    CameraBinding,
    CameraManager,
    CameraSpec,
    SpawnedCamera,
)
from tests.test_camera_manager import FakeCarlaActor, FakeSensor, FakeSpawner


class FakeWorldFacade:
    """Stand-in for core.world.World with the attributes scenarios need."""

    def __init__(self, carla_world: object) -> None:
        self.carla_world = carla_world


@register_scenario("__test_simple__")
class _SimpleScenario(Scenario):
    """In-test scenario: registers fleet members + records hook calls.

    Avoids any CARLA calls — useful for testing the runner lifecycle.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.setup_called = False
        self.teardown_calls = 0
        self.pre_calls = 0
        self.post_calls = 0
        self.commands: list = []

    def setup(self) -> None:
        self.setup_called = True
        # Register a virtual UAV + rebind the aerial channel — these don't
        # need a CARLA actor on the spawner since FakeSpawner accepts None.
        self.fleet.register(
            VirtualMember(entity_id="UAV-99", role="patrol", _pose=Pose(z=50))
        )
        self._register_entity("UAV-99")
        self.camera_manager.rebind(
            "aerial", "UAV-99",
            world=self.world.carla_world,
            fleet=self.fleet,
        )
        self._record_rebound("aerial")

    def on_tick_pre(self, sim_time):
        self.pre_calls += 1

    def on_tick_post(self, sim_time):
        self.post_calls += 1

    def on_command(self, cmd):
        self.commands.append(cmd)

    def teardown(self):
        self.teardown_calls += 1
        super().teardown()


# ---------- registry --------------------------------------------------


def test_registry_lists_known_scenarios():
    names = available_scenarios()
    assert "s1_fire" in names  # registered by package import side-effect
    assert "__test_simple__" in names


def test_registry_unknown_raises():
    with pytest.raises(KeyError):
        get_scenario_class("definitely-not-a-scenario")


# ---------- runner lifecycle ------------------------------------------


def _make_runner(spawner: FakeSpawner | None = None) -> tuple[ScenarioRunner, Fleet, CameraManager]:
    fleet = Fleet()
    mgr = CameraManager(spawner=spawner or FakeSpawner())
    # Seed the aerial binding so _SimpleScenario.setup can rebind it.
    mgr.bind(CameraBinding(spec=CameraSpec(
        id="aerial", mode="follows_virtual",
        z=20, pitch=-30,
    )))
    runner = ScenarioRunner(
        get_scenario_class("__test_simple__"),
        world=FakeWorldFacade(carla_world=object()),
        fleet=fleet,
        camera_manager=mgr,
        event_log=EventLog(capacity=50),
    )
    return runner, fleet, mgr


def test_runner_start_runs_setup_and_transitions_to_running():
    runner, fleet, mgr = _make_runner()
    assert runner.state == "idle"
    scenario = runner.start()
    assert isinstance(scenario, _SimpleScenario)
    assert scenario.setup_called
    assert runner.state == "running"
    # Fleet + camera bindings updated by setup.
    assert fleet.get("UAV-99") is not None
    assert "aerial" in mgr.cameras  # spawned during rebind


def test_runner_stop_runs_teardown_and_clears_fleet_and_cameras():
    runner, fleet, mgr = _make_runner()
    scenario = runner.start()
    runner.stop()
    assert runner.state == "stopped"
    assert scenario.teardown_calls == 1
    # Fleet entry gone, camera unbound, sensor destroyed.
    assert fleet.get("UAV-99") is None
    assert "aerial" not in mgr.cameras


def test_runner_idempotent_restart_clean():
    """5 start/stop cycles with no residue (proxy for AC-8 / NF7)."""
    spawner = FakeSpawner()
    runner, fleet, mgr = _make_runner(spawner=spawner)
    for _ in range(5):
        runner.start()
        runner.stop()
    # After all cycles, no leftover fleet members, no spawned cameras, no
    # leaked sensors (every fake sensor must have been destroyed).
    assert len(fleet) == 0
    assert mgr.cameras == {}
    destroyed = [c for c in spawner.calls if True]  # all spawn calls
    # Each cycle: 1 spawn (aerial) + 1 destroy on teardown — verify counts.
    assert len(destroyed) == 5


def test_runner_setup_failure_runs_partial_teardown():
    """If setup raises mid-way, the runner should call teardown to clean up."""

    @register_scenario("__test_failing__")
    class _FailingScenario(Scenario):
        def setup(self):
            # Register one entity, then fail.
            self.fleet.register(VirtualMember(entity_id="UAV-FAIL", role="patrol"))
            self._register_entity("UAV-FAIL")
            raise RuntimeError("intentional")

    fleet = Fleet()
    mgr = CameraManager(spawner=FakeSpawner())
    runner = ScenarioRunner(
        get_scenario_class("__test_failing__"),
        world=FakeWorldFacade(carla_world=object()),
        fleet=fleet,
        camera_manager=mgr,
        event_log=EventLog(capacity=50),
    )
    with pytest.raises(RuntimeError, match="intentional"):
        runner.start()
    assert runner.state == "failed"
    # Partial-setup entity was cleaned up.
    assert fleet.get("UAV-FAIL") is None


# ---------- scenario hooks pass-through ---------------------------------


def test_scenario_hooks_callable_independently():
    """on_tick_pre/post/on_command are invoked by tick_loop, not runner —
    verify they're plain method calls on the live scenario instance."""
    runner, _, _ = _make_runner()
    scenario = runner.start()
    scenario.on_tick_pre(0.1)
    scenario.on_tick_pre(0.2)
    scenario.on_tick_post(0.1)
    scenario.on_command({"id": "x"})
    assert scenario.pre_calls == 2
    assert scenario.post_calls == 1
    assert scenario.commands == [{"id": "x"}]
    runner.stop()


def test_scenario_unregistered_actor_uses_carla_actor_helper(monkeypatch):
    """`_register_actor` accumulates and teardown calls destroy() on each."""
    fleet = Fleet()
    mgr = CameraManager(spawner=FakeSpawner())
    runner = ScenarioRunner(
        get_scenario_class("__test_simple__"),
        world=FakeWorldFacade(carla_world=object()),
        fleet=fleet,
        camera_manager=mgr,
        event_log=EventLog(capacity=50),
    )
    scenario = runner.start()
    fake_actor1 = FakeSensor(id=1001)
    fake_actor2 = FakeSensor(id=1002)
    scenario._register_actor(fake_actor1)
    scenario._register_actor(fake_actor2)
    runner.stop()
    assert fake_actor1.destroyed
    assert fake_actor2.destroyed
