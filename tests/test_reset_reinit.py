"""S1FireScenario.reset() — teardown + setup (R4-04)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from carlabridge.commands.bus import CommandBus
from carlabridge.commands.enum import CommandKind, ParsedCommand
from carlabridge.core.fleet import CarlaActorMember, Fleet
from carlabridge.obs.event_log import EventLog
from carlabridge.scenarios.s1_fire import S1FireScenario
from carlabridge.sensors.camera import CameraManager
from tests.spawn_config import make_spawn_settings
from tests.test_camera_manager import FakeSpawner


# ---- shared CARLA-like fakes ---------------------------------------------


@dataclass
class _FakeLoc:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


@dataclass
class _FakeRot:
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0


@dataclass
class _FakeTransform:
    location: _FakeLoc
    rotation: _FakeRot


class _FakeUgvActor:
    next_id = 1000

    def __init__(self, bp_id: str, transform: _FakeTransform) -> None:
        _FakeUgvActor.next_id += 1
        self.id = _FakeUgvActor.next_id
        self.type_id = bp_id
        self.transform = transform
        self.destroyed = False
        self.applied = []

    def get_transform(self) -> _FakeTransform:
        return self.transform

    def get_velocity(self):
        from tests.fakes.fake_world import FakeVelocity
        return FakeVelocity()

    def apply_control(self, c) -> None:
        self.applied.append(c)

    def destroy(self) -> None:
        self.destroyed = True


@dataclass
class _FakeBp:
    id: str


class _FakeBpLib:
    def filter(self, q: str) -> list[_FakeBp]:
        # `vehicle.*` and the named UGV candidates must all yield at least one.
        return [_FakeBp(q)] if q else []


class _FakeMap:
    def get_spawn_points(self) -> list[_FakeTransform]:
        return [_FakeTransform(_FakeLoc(10.0, 20.0, 0.0), _FakeRot(0, 0, 0))]


class _FakeCarlaWorld:
    def __init__(self) -> None:
        self.spawned: list[_FakeUgvActor] = []
        self._map = _FakeMap()
        self._lib = _FakeBpLib()

    def get_map(self) -> _FakeMap:
        return self._map

    def get_blueprint_library(self) -> _FakeBpLib:
        return self._lib

    def try_spawn_actor(self, bp: _FakeBp, tf: _FakeTransform) -> _FakeUgvActor | None:
        a = _FakeUgvActor(bp.id, tf)
        self.spawned.append(a)
        return a


@dataclass
class _WorldFacade:
    carla_world: Any


# ---- factory ------------------------------------------------------------


def _make_scen() -> tuple[S1FireScenario, _FakeCarlaWorld, CameraManager, CommandBus, list[dict]]:
    carla = _FakeCarlaWorld()
    cam = CameraManager(spawner=FakeSpawner())
    # Pre-create channels so rebind doesn't no-op.
    from carlabridge.sensors.camera import CameraBinding, CameraSpec
    cam.bind(CameraBinding(spec=CameraSpec(
        id="aerial", mode="follows_virtual",
        x=0, y=0, z=20, pitch=-30,
    )))
    cam.bind(CameraBinding(spec=CameraSpec(
        id="ground", mode="attached_to_actor",
        x=-3, y=0, z=2, pitch=-10,
    )))
    bus = CommandBus(maxsize=8)
    statuses: list[dict] = []
    bus.set_on_command_status(statuses.append)
    settings = make_spawn_settings()
    scen = S1FireScenario(
        world=_WorldFacade(carla),
        fleet=Fleet(),
        camera_manager=cam,
        event_log=EventLog(capacity=200),
        command_bus=bus,
        settings=settings,
    )
    scen.attach_sim_time_provider(lambda: 0.0)
    scen.setup()
    return scen, carla, cam, bus, statuses


# ---- core reset invariants ----------------------------------------------


def test_reset_increments_run_id():
    scen, _, _, _, _ = _make_scen()
    assert scen._run_id == 0
    r = scen.reset()
    assert scen._run_id == 1
    assert r["new_run_id"] == 1
    scen.reset()
    assert scen._run_id == 2


def test_reset_cancels_all_in_flight():
    scen, _, _, _, statuses = _make_scen()

    class _FakeFollower:
        def set_destination(self, *a, **kw): pass
        def run_step(self): return None
        def done(self): return False

    scen._make_follower = lambda _a, _m: _FakeFollower()  # type: ignore[method-assign]
    scen._set_destination = lambda _f, _d: None           # type: ignore[method-assign]

    scen.on_command(ParsedCommand(id="cmd-01", kind=CommandKind.UAV_GOTO, target="UAV-01",
                                  params={"waypoint": {"x": 100, "y": 0, "z": 60},
                                          "cruise_speed": 1.0}))
    scen.on_command(ParsedCommand(id="cmd-02", kind=CommandKind.UGV_GOTO, target="UGV-01",
                                  params={"dest": {"x": 200, "y": 0, "z": 0},
                                          "target_speed": 25.0}))
    r = scen.reset()
    assert set(r["cancelled_commands"]) == {"cmd-01", "cmd-02"}
    cancellations = [s for s in statuses if s["status"] == "cancelled"]
    assert {c["cmd_id"] for c in cancellations} == {"cmd-01", "cmd-02"}
    assert all(c["reason"] == "reset" for c in cancellations)
    assert scen._in_flight == {}


def test_reset_lists_destroyed_incidents():
    scen, _, _, _, _ = _make_scen()
    scen.ignite_fire(id="fire-001", position={"x": 30, "y": 0, "z": 0})
    scen.ignite_fire(id="fire-002", position={"x": 40, "y": 0, "z": 0})
    r = scen.reset()
    assert set(r["destroyed_incidents"]) == {"fire-001", "fire-002"}
    assert scen.fleet.incidents() == {}


def test_reset_re_spawns_actor_with_new_carla_id():
    scen, carla, _, _, _ = _make_scen()
    original_actor = scen.fleet.get("UGV-01")
    assert isinstance(original_actor, CarlaActorMember)
    original_actor_id = original_actor.actor.id
    assert original_actor.actor.destroyed is False
    scen.reset()
    new_actor = scen.fleet.get("UGV-01")
    assert isinstance(new_actor, CarlaActorMember)
    # entity_id stable; CARLA actor id changed; old actor destroyed.
    assert new_actor.actor.id != original_actor_id
    assert original_actor.actor.destroyed is True


def test_reset_preserves_camera_frame_queue_identity():
    scen, _, cam, _, _ = _make_scen()
    aerial_q_before = cam.queue_for("aerial")
    ground_q_before = cam.queue_for("ground")
    assert aerial_q_before is not None
    assert ground_q_before is not None
    scen.reset()
    aerial_q_after = cam.queue_for("aerial")
    ground_q_after = cam.queue_for("ground")
    # Same FrameQueue python object — WebRTC track keeps working.
    assert aerial_q_after is aerial_q_before
    assert ground_q_after is ground_q_before


def test_reset_resets_resetting_flag():
    scen, _, _, _, _ = _make_scen()
    assert scen._resetting is False
    scen.reset()
    assert scen._resetting is False


def test_reset_refills_origins_for_all_entities():
    scen, _, _, _, _ = _make_scen()
    before = scen.fleet.origins()
    scen.reset()
    after = scen.fleet.origins()
    assert set(after.keys()) == set(before.keys())
    assert set(after.keys()) == {"UGV-01", "UAV-01"}


def test_reset_during_reset_raises():
    scen, _, _, _, _ = _make_scen()
    scen._resetting = True
    with pytest.raises(RuntimeError):
        scen.reset()


def test_reset_keeps_run_id_increment_even_if_setup_runs_clean():
    scen, _, _, _, _ = _make_scen()
    scen.ignite_fire(id="fire-001", position={"x": 30, "y": 0, "z": 0})
    scen.reset()
    # After reset, no incidents, no in-flight, fresh run.
    assert scen.fleet.incidents() == {}
    assert scen._in_flight == {}
    assert scen._run_id == 1


def test_reset_cancellation_carries_trigger_detail():
    scen, _, _, _, statuses = _make_scen()
    scen.on_command(ParsedCommand(id="cmd-01", kind=CommandKind.UAV_GOTO,
                                  target="UAV-01",
                                  params={"waypoint": {"x": 999, "y": 0, "z": 60},
                                          "cruise_speed": 1.0}))
    scen.reset()
    cancellation = [s for s in statuses if s["status"] == "cancelled"][0]
    assert cancellation["reason"] == "reset"
    assert cancellation["detail"] == {"trigger": "http"}
