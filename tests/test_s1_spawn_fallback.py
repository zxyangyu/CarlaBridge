"""S1 UGV spawn: configured pose failure falls back to map spawn points."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from carlabridge.core.fleet import Fleet
from carlabridge.obs.event_log import EventLog
from carlabridge.scenarios.s1_fire import S1FireScenario
from carlabridge.sensors.camera import CameraManager, CameraBinding, CameraSpec
from tests.spawn_config import make_spawn_settings
from tests.test_camera_manager import FakeSpawner
from tests.test_reset_reinit import (
    _FakeBp,
    _FakeBpLib,
    _FakeLoc,
    _FakeMap,
    _FakeRot,
    _FakeTransform,
    _FakeUgvActor,
    _WorldFacade,
)


@dataclass
class _FakeMapWithSpawn(_FakeMap):
    def get_spawn_points(self) -> list[_FakeTransform]:
        return [_FakeTransform(_FakeLoc(10.0, 20.0, 0.5), _FakeRot(yaw=45.0))]


class _FakeWorldRejectOrigin:
    """try_spawn_actor fails at (0,0,0) but succeeds at map spawn points."""

    def __init__(self) -> None:
        self.spawned: list[_FakeUgvActor] = []
        self._map = _FakeMapWithSpawn()
        self._lib = _FakeBpLib()

    def get_map(self) -> _FakeMapWithSpawn:
        return self._map

    def get_blueprint_library(self) -> _FakeBpLib:
        return self._lib

    def try_spawn_actor(self, bp: _FakeBp, tf: Any) -> _FakeUgvActor | None:
        loc = tf.location
        if abs(loc.x) < 1e-3 and abs(loc.y) < 1e-3:
            return None
        actor = _FakeUgvActor(bp.id, tf)
        self.spawned.append(actor)
        return actor


def _make_cam() -> CameraManager:
    cam = CameraManager(spawner=FakeSpawner())
    cam.bind(CameraBinding(spec=CameraSpec(
        id="aerial", mode="follows_virtual", x=0, y=0, z=20, pitch=-30,
    )))
    cam.bind(CameraBinding(spec=CameraSpec(
        id="ground", mode="attached_to_actor", x=-3, y=0, z=2, pitch=-10,
    )))
    return cam


def test_setup_falls_back_to_map_spawn_point():
    carla = _FakeWorldRejectOrigin()
    settings = make_spawn_settings(vehicle=(0.0, 0.0, 0.0, 0.0))
    scen = S1FireScenario(
        world=_WorldFacade(carla),
        fleet=Fleet(),
        camera_manager=_make_cam(),
        event_log=EventLog(capacity=50),
        settings=settings,
    )
    scen.setup()

    assert len(carla.spawned) == 1
    origin = scen.fleet.get_origin("UGV-01")
    assert origin is not None
    assert origin.x == pytest.approx(10.0)
    assert origin.y == pytest.approx(20.0)
    assert origin.z == pytest.approx(0.5)
    assert origin.yaw == pytest.approx(45.0)
    assert scen._anchor_world_xyz == pytest.approx((10.0, 20.0, 0.5))


def test_setup_raises_when_configured_and_spawn_points_fail():
    class _AlwaysFail(_FakeWorldRejectOrigin):
        def try_spawn_actor(self, bp: _FakeBp, tf: Any) -> None:
            return None

    settings = make_spawn_settings()
    scen = S1FireScenario(
        world=_WorldFacade(_AlwaysFail()),
        fleet=Fleet(),
        camera_manager=_make_cam(),
        event_log=EventLog(capacity=50),
        settings=settings,
    )
    with pytest.raises(RuntimeError, match="configured pose.*map spawn point"):
        scen.setup()
