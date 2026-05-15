"""HTTP control plane: /scenario/fire, /scenario/reset, /scenario/status (R6-02..05)."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from carlabridge.bus.projector import FocusBinding
from carlabridge.bus.server import build_app, make_sio
from carlabridge.commands.bus import CommandBus
from carlabridge.config import Settings
from carlabridge.core.atomic import AtomicRef
from carlabridge.core.fleet import Fleet
from carlabridge.core.snapshot import WorldSnapshot
from carlabridge.obs.event_log import EventLog
from carlabridge.obs.metrics import Metrics
from carlabridge.scenarios.runner import ScenarioRunner
from carlabridge.scenarios.s1_fire import S1FireScenario
from carlabridge.sensors.camera import CameraBinding, CameraManager, CameraSpec
from tests.test_camera_manager import FakeSpawner


# ---- CARLA-shaped fakes (mirror test_reset_reinit) ------------------------


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

    def get_map(self): return _Map()
    def get_blueprint_library(self): return _BpLib()
    def try_spawn_actor(self, _bp, _tf) -> _Actor:
        self._next += 1
        self.spawn_count += 1
        return _Actor(self._next)


@dataclass
class _Facade:
    carla_world: Any


# ---- shared fixture: real aiohttp + Socket.IO + scenario_runner ----------


@pytest.fixture
async def http_bridge():
    """Spin a full HTTP server with a live scenario_runner + drain thread."""
    settings = Settings()
    event_log = EventLog(capacity=500)
    metrics = Metrics()
    sio = make_sio(settings)
    loop = asyncio.get_event_loop()

    bus = CommandBus(loop=loop, sio=sio, event_log=event_log)
    cam = CameraManager(spawner=FakeSpawner())
    cam.bind(CameraBinding(spec=CameraSpec(id="aerial", mode="follows_virtual",
                                           x=0, y=0, z=20, pitch=-30)))
    cam.bind(CameraBinding(spec=CameraSpec(id="ground", mode="attached_to_actor",
                                           x=-3, y=0, z=2, pitch=-10)))

    app, _ = build_app(
        settings, event_log, metrics,
        sio=sio,
        snapshot_ref=AtomicRef[WorldSnapshot](),
        focus=FocusBinding(),
        camera_manager=cam,
        command_bus=bus,
        bridge_session_id="br-test",
        scenario_name="s1_fire",
    )

    runner = ScenarioRunner(
        S1FireScenario,
        world=_Facade(_CarlaWorld()),
        fleet=Fleet(),
        camera_manager=cam,
        event_log=event_log,
        command_bus=bus,
    )
    runner.start()
    app["late"]["scenario_runner"] = runner
    app["agent_ns"].set_resetting_provider(runner.is_resetting)

    # Background drainer simulating the tick thread (calls drain ~100 Hz).
    stop = threading.Event()

    def _drain_loop():
        while not stop.is_set():
            runner.drain_sim_tasks()
            stop.wait(0.01)

    drainer = threading.Thread(target=_drain_loop, daemon=True)
    drainer.start()

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, runner
    finally:
        stop.set()
        drainer.join(timeout=1.0)
        await client.close()
        try:
            runner.stop()
        except Exception:
            pass


# ---- /scenario/fire ------------------------------------------------------


async def test_fire_happy_path(http_bridge):
    client, runner = http_bridge
    resp = await client.post("/scenario/fire", json={
        "id": "fire-001",
        "position": {"x": 30.0, "y": 0.0, "z": 0.0},
    })
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"
    assert body["incident_id"] == "fire-001"
    assert body["position"] == {"x": 30.0, "y": 0.0, "z": 0.0}
    assert runner.scenario.fleet.get_incident("fire-001") is not None


async def test_fire_auto_id_when_omitted(http_bridge):
    client, _ = http_bridge
    resp = await client.post("/scenario/fire", json={
        "position": {"x": 10.0, "y": 0.0, "z": 0.0},
    })
    assert resp.status == 200
    body = await resp.json()
    assert body["incident_id"].startswith("fire-")


async def test_fire_missing_position_400(http_bridge):
    client, _ = http_bridge
    resp = await client.post("/scenario/fire", json={"id": "fire-x"})
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "parse_error"


async def test_fire_invalid_position_axis_400(http_bridge):
    client, _ = http_bridge
    resp = await client.post("/scenario/fire", json={
        "id": "fire-x", "position": {"x": "oops", "y": 0.0},
    })
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "parse_error"


async def test_fire_duplicate_id_409(http_bridge):
    client, _ = http_bridge
    first = await client.post("/scenario/fire", json={
        "id": "fire-dup", "position": {"x": 0.0, "y": 0.0, "z": 0.0},
    })
    assert first.status == 200
    second = await client.post("/scenario/fire", json={
        "id": "fire-dup", "position": {"x": 1.0, "y": 2.0, "z": 0.0},
    })
    assert second.status == 409
    body = await second.json()
    assert body["reason"] == "duplicate_incident"


async def test_fire_during_reset_503(http_bridge):
    client, runner = http_bridge
    runner.scenario._resetting = True
    try:
        resp = await client.post("/scenario/fire", json={
            "id": "fire-x", "position": {"x": 0.0, "y": 0.0, "z": 0.0},
        })
    finally:
        runner.scenario._resetting = False
    assert resp.status == 503
    body = await resp.json()
    assert body["reason"] == "scenario_resetting"


async def test_fire_non_json_body_400(http_bridge):
    client, _ = http_bridge
    resp = await client.post("/scenario/fire", data="not json")
    assert resp.status == 400


# ---- /scenario/reset -----------------------------------------------------


async def test_reset_returns_cancelled_and_destroyed(http_bridge):
    client, runner = http_bridge
    # Pre-populate with an incident + an in-flight command.
    await client.post("/scenario/fire", json={
        "id": "fire-001", "position": {"x": 30.0, "y": 0.0, "z": 0.0},
    })
    from carlabridge.commands.enum import CommandKind, ParsedCommand
    runner.scenario.on_command(ParsedCommand(
        id="cmd-A", kind=CommandKind.UAV_GOTO, target="UAV-01",
        params={"waypoint": {"x": 999, "y": 0, "z": 60}, "cruise_speed": 1.0},
    ))

    resp = await client.post("/scenario/reset", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"
    assert body["run_id"] == 1
    assert "cmd-A" in body["cancelled_commands"]
    assert "fire-001" in body["destroyed_incidents"]


async def test_reset_run_id_monotonic(http_bridge):
    client, _ = http_bridge
    r1 = await (await client.post("/scenario/reset", json={})).json()
    r2 = await (await client.post("/scenario/reset", json={})).json()
    assert r1["run_id"] == 1
    assert r2["run_id"] == 2


# ---- /scenario/status ----------------------------------------------------


async def test_status_baseline_after_setup(http_bridge):
    client, _ = http_bridge
    resp = await client.get("/scenario/status")
    assert resp.status == 200
    body = await resp.json()
    assert body["name"] == "s1_fire"
    assert body["run_id"] == 0
    assert body["resetting"] is False
    assert body["bridge_session_id"] == "br-test"
    assert body["incidents"] == []
    assert body["in_flight_commands"] == []
    # Entities seeded by setup.
    assert {"UGV-01", "UAV-01", "UAV-02", "UAV-03"} <= set(body["entities"].keys())
    # Origins recorded.
    assert "origin" in body["entities"]["UGV-01"]


async def test_status_reflects_incident_after_fire(http_bridge):
    client, _ = http_bridge
    await client.post("/scenario/fire", json={
        "id": "fire-001", "position": {"x": 30.0, "y": 0.0, "z": 0.0},
    })
    body = await (await client.get("/scenario/status")).json()
    assert len(body["incidents"]) == 1
    assert body["incidents"][0]["id"] == "fire-001"


async def test_status_no_sim_hop_works_during_reset(http_bridge):
    client, runner = http_bridge
    runner.scenario._resetting = True
    try:
        resp = await client.get("/scenario/status")
    finally:
        runner.scenario._resetting = False
    assert resp.status == 200
    body = await resp.json()
    assert body["resetting"] is True


# ---- R6-05 lockout: agent.command rejected during reset ------------------


async def test_agent_command_rejected_while_resetting(http_bridge):
    client, runner = http_bridge
    import socketio
    runner.scenario._resetting = True
    try:
        sio_client = socketio.AsyncClient()
        await sio_client.connect(
            f"http://127.0.0.1:{client.server.port}", namespaces=["/agent"],
        )
        try:
            ack = await sio_client.call(
                "agent.command",
                {"id": "cmd-block", "kind": "UAV_HOLD", "target": "UAV-01"},
                namespace="/agent", timeout=2,
            )
            assert ack["status"] == "rejected"
            assert ack["reason"] == "scenario_resetting"
            assert ack["cmd_id"] == "cmd-block"
        finally:
            await sio_client.disconnect()
    finally:
        runner.scenario._resetting = False
