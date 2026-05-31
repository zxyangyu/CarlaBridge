"""Bridge is time-independent (design §1, §6.2): no sim_time-driven events.

100s of sim_time worth of ``on_tick_post`` must NOT trigger:

* ``ignite_fire`` (only HTTP /scenario/fire)
* ``reset``        (only HTTP /scenario/reset)
* ``command_status`` (no commands accepted → nothing to broadcast)
* ``scenario_event`` (only HTTP triggers it)

The scenario stays passive: UAVs hover at their origins, UGV stays still,
no incident appears, no command is auto-issued.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from carlabridge.commands.bus import CommandBus
from carlabridge.core.fleet import Fleet
from carlabridge.obs.event_log import EventLog
from carlabridge.scenarios.s1_fire import S1FireScenario
from carlabridge.sensors.camera import CameraBinding, CameraManager, CameraSpec
from tests.spawn_config import make_spawn_settings
from tests.test_camera_manager import FakeSpawner


# ---- CARLA fakes (reuse the shape from test_reset_reinit) -----------------


@dataclass
class _Loc: x: float = 0; y: float = 0; z: float = 0


@dataclass
class _Rot: yaw: float = 0; pitch: float = 0; roll: float = 0


@dataclass
class _Tf:
    location: _Loc
    rotation: _Rot


class _Bp:
    def __init__(self, idv: str) -> None:
        self.id = idv


class _BpLib:
    def filter(self, q: str) -> list[_Bp]:
        return [_Bp(q)] if q else []


class _Actor:
    def __init__(self, idv: int) -> None:
        self.id = idv
        self.type_id = "vehicle.fake"
        self.destroyed = False

    def destroy(self) -> None: self.destroyed = True
    def get_transform(self): return _Tf(_Loc(), _Rot())
    def get_velocity(self):
        from tests.fakes.fake_world import FakeVelocity
        return FakeVelocity()
    def apply_control(self, _c) -> None: pass


class _Map:
    def get_spawn_points(self) -> list[_Tf]:
        return [_Tf(_Loc(0, 0, 0), _Rot(0, 0, 0))]


class _CarlaWorld:
    def __init__(self) -> None:
        self._next = 1
        self.spawn_count = 0

    def get_map(self) -> _Map: return _Map()
    def get_blueprint_library(self) -> _BpLib: return _BpLib()
    def try_spawn_actor(self, _bp, _tf) -> _Actor:
        self._next += 1
        self.spawn_count += 1
        return _Actor(self._next)


@dataclass
class _Facade:
    carla_world: Any


def _make_scen() -> tuple[S1FireScenario, CommandBus, list[dict], EventLog]:
    cam = CameraManager(spawner=FakeSpawner())
    cam.bind(CameraBinding(spec=CameraSpec(id="aerial", mode="follows_virtual",
                                           x=0, y=0, z=20, pitch=-30)))
    cam.bind(CameraBinding(spec=CameraSpec(id="ground", mode="attached_to_actor",
                                           x=-3, y=0, z=2, pitch=-10)))
    bus = CommandBus(maxsize=8)
    statuses: list[dict] = []
    bus.set_on_command_status(statuses.append)
    evlog = EventLog(capacity=2000)
    settings = make_spawn_settings()
    scen = S1FireScenario(
        world=_Facade(_CarlaWorld()),
        fleet=Fleet(),
        camera_manager=cam,
        event_log=evlog,
        command_bus=bus,
        settings=settings,
    )
    scen.setup()
    return scen, bus, statuses, evlog


# ---- the time-independence assertion -------------------------------------


def test_100s_of_ticks_emits_no_command_status():
    scen, bus, statuses, _ = _make_scen()
    # Snapshot baseline events from setup (3 ok logs etc.) — we only check
    # that NO additional events fire while we tick the world.
    baseline_events = len(scen.event_log.recent())
    # 30 Hz × 100s = 3000 ticks.
    for tick in range(3000):
        sim_time = tick / 30.0
        scen.on_tick_post(sim_time)
    # No command_status broadcasts.
    assert statuses == []
    # No new incidents (fleet remains empty).
    assert scen.fleet.incidents() == {}
    # No new in-flight commands (none ever accepted).
    assert scen._in_flight == {}
    # No additional event_log entries beyond setup baseline.
    assert len(scen.event_log.recent()) == baseline_events


def test_100s_of_ticks_does_not_invoke_ignite_or_reset(monkeypatch):
    scen, _, _, _ = _make_scen()
    ignite_calls: list[Any] = []
    reset_calls: list[Any] = []
    monkeypatch.setattr(scen, "ignite_fire",
                        lambda *a, **kw: ignite_calls.append((a, kw)))
    monkeypatch.setattr(scen, "reset",
                        lambda *a, **kw: reset_calls.append((a, kw)))
    for tick in range(3000):
        scen.on_tick_post(tick / 30.0)
    assert ignite_calls == []
    assert reset_calls == []


def test_100s_of_ticks_does_not_change_run_id():
    scen, _, _, _ = _make_scen()
    assert scen._run_id == 0
    for tick in range(3000):
        scen.on_tick_post(tick / 30.0)
    assert scen._run_id == 0


def test_no_auto_target_on_uavs_after_setup():
    """Old setup auto-armed UAV-01 toward the fire marker. Refactor v0.3
    forbids that — UAVs only move when an Agent sends PATROL/GOTO."""
    scen, _, _, _ = _make_scen()
    for eid in ("UAV-01",):
        uav = scen.fleet.get(eid)
        assert uav.target is None, f"{eid} has implicit target {uav.target}"
