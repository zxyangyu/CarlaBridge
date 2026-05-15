"""S1FireScenario.ignite_fire (R4-03)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from carlabridge.core.fleet import CarlaActorMember, Fleet, Pose, VirtualMember
from carlabridge.obs.event_log import EventLog
from carlabridge.scenarios.s1_fire import S1FireScenario
from carlabridge.sensors.camera import CameraManager
from tests.test_camera_manager import FakeSpawner


# ---- fakes for fire spawn ------------------------------------------------


@dataclass
class _FakeBlueprint:
    id: str


class _FakeBpLib:
    def __init__(self, present: tuple[str, ...]) -> None:
        self._present = present

    def filter(self, bp_id: str) -> list[_FakeBlueprint]:
        return [_FakeBlueprint(bp_id)] if bp_id in self._present else []


@dataclass
class _FakeFireActor:
    id: int
    bp_id: str
    transform: Any
    destroyed: bool = False

    def destroy(self) -> None:
        self.destroyed = True


class _FakeCarlaWorld:
    def __init__(self, *, blueprints: tuple[str, ...], fail_spawn: bool = False) -> None:
        self._bp_lib = _FakeBpLib(blueprints)
        self._fail = fail_spawn
        self._next_id = 1000
        self.spawned: list[_FakeFireActor] = []

    def get_blueprint_library(self) -> _FakeBpLib:
        return self._bp_lib

    def try_spawn_actor(self, bp: _FakeBlueprint, transform: Any):
        if self._fail:
            return None
        a = _FakeFireActor(id=self._next_id, bp_id=bp.id, transform=transform)
        self._next_id += 1
        self.spawned.append(a)
        return a


@dataclass
class _WorldFacade:
    carla_world: Any


def _make_scen(blueprints=("vehicle.carlamotors.firetruck",), fail_spawn=False):
    fleet = Fleet()
    # Pre-populate UGV/UAV to mimic post-setup state; ignite_fire doesn't
    # need them but keeping the fleet realistic is harmless.
    fleet.register(CarlaActorMember(
        entity_id="UGV-01", role="dispatchable",
        actor=type("A", (), {"id": 1, "type_id": "x", "destroy": lambda self: None,
                              "get_transform": lambda self: None,
                              "get_velocity": lambda self: None,
                              "apply_control": lambda self, c: None})(),
    ))
    fleet.set_origin("UGV-01", Pose(x=0, y=0, z=0))
    carla = _FakeCarlaWorld(blueprints=blueprints, fail_spawn=fail_spawn)
    scen = S1FireScenario(
        world=_WorldFacade(carla),
        fleet=fleet,
        camera_manager=CameraManager(spawner=FakeSpawner()),
        event_log=EventLog(capacity=200),
    )
    scen._anchor_world_xyz = (0.0, 0.0, 0.0)
    scen._anchor_yaw = 0.0
    return scen, fleet, carla


# ---- success -------------------------------------------------------------


def test_ignite_fire_registers_incident_and_spawns_actor():
    scen, fleet, carla = _make_scen()
    incident = scen.ignite_fire(id="fire-001", position={"x": 90, "y": 0, "z": 0})
    assert fleet.get_incident("fire-001") is incident
    assert "fire-001" in scen._fire_actors
    assert len(carla.spawned) == 1


def test_ignite_fire_auto_id_when_omitted():
    scen, _, _ = _make_scen()
    inc = scen.ignite_fire(position={"x": 10, "y": 0, "z": 0})
    assert inc.id.startswith("fire-")
    # Auto id is monotonic.
    inc2 = scen.ignite_fire(position={"x": 11, "y": 0, "z": 0})
    assert inc2.id != inc.id


def test_ignite_fire_preserves_severity_and_kind():
    scen, _, _ = _make_scen()
    inc = scen.ignite_fire(id="fire-007", position={"x": 5, "y": 0, "z": 0},
                           severity="low", kind="fire")
    assert inc.severity == "low"
    assert inc.kind == "fire"


def test_ignite_fire_uses_explicit_blueprint_when_provided():
    scen, _, carla = _make_scen(blueprints=("static.prop.kiosk_01",))
    scen.ignite_fire(id="fire-001", position={"x": 1, "y": 2, "z": 0},
                     blueprint="static.prop.kiosk_01")
    assert carla.spawned[0].bp_id == "static.prop.kiosk_01"


def test_ignite_fire_falls_back_through_default_blueprints():
    # First two default blueprints absent; third present.
    scen, _, carla = _make_scen(blueprints=("static.prop.kiosk_01",))
    scen.ignite_fire(id="fire-001", position={"x": 0, "y": 0, "z": 0})
    assert carla.spawned[0].bp_id == "static.prop.kiosk_01"


# ---- error cases ---------------------------------------------------------


def test_ignite_fire_duplicate_id_raises_valueerror():
    scen, _, _ = _make_scen()
    scen.ignite_fire(id="fire-001", position={"x": 0, "y": 0, "z": 0})
    with pytest.raises(ValueError):
        scen.ignite_fire(id="fire-001", position={"x": 1, "y": 1, "z": 0})


def test_ignite_fire_all_blueprints_fail_raises_runtimeerror():
    scen, _, _ = _make_scen(blueprints=(), fail_spawn=True)
    with pytest.raises(RuntimeError):
        scen.ignite_fire(id="fire-001", position={"x": 0, "y": 0, "z": 0})


def test_ignite_fire_during_reset_raises():
    scen, _, _ = _make_scen()
    scen._resetting = True
    with pytest.raises(RuntimeError):
        scen.ignite_fire(id="fire-001", position={"x": 0, "y": 0, "z": 0})


def test_ignite_fire_position_accepts_pose_object():
    scen, _, _ = _make_scen()
    inc = scen.ignite_fire(id="fire-pose", position=Pose(x=3, y=4, z=5))
    assert inc.position.x == 3
    assert inc.position.y == 4
    assert inc.position.z == 5


def test_ignite_fire_event_log_records_warning():
    scen, _, _ = _make_scen()
    scen.ignite_fire(id="fire-001", position={"x": 0, "y": 0, "z": 0})
    events = scen.event_log.recent()
    msgs = [e.message for e in events if e.severity == "warn"]
    assert any("fire-001" in m for m in msgs)
