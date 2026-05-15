"""End-to-end: HOLD / STOP complete quickly; UGV_EXTINGUISH after dwell.

A live aiohttp + Socket.IO server runs with the real S1FireScenario over a
fake CARLA world. A "ticker" daemon thread simulates the tick loop:

* drains :class:`CommandBus` → forwards commands into ``scenario.on_command``
* drains :class:`ScenarioRunner` sim-task queue
* calls ``scenario.on_tick_post`` so the lifecycle framework finalises any
  ``awaiting == "instant"`` commands and, after ``EXTINGUISH_DWELL_S`` sim
  seconds, ``awaiting == "extinguish"`` commands

We then issue ``sio.call('agent.command', ...)`` from a Socket.IO client and
assert the matching ``command_status: completed`` event arrives within the
expected time budget (R8-04 DoD).
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any

import pytest
import socketio
from aiohttp.test_utils import TestClient, TestServer

from carlabridge.bus.projector import FocusBinding
from carlabridge.bus.server import build_app, make_sio
from carlabridge.commands.bus import CommandBus
from carlabridge.config import Settings
from carlabridge.core.atomic import AtomicRef
from carlabridge.core.fleet import Fleet, Pose
from carlabridge.core.incident import Incident
from carlabridge.core.snapshot import WorldSnapshot
from carlabridge.obs.event_log import EventLog
from carlabridge.obs.metrics import Metrics
from carlabridge.scenarios.runner import ScenarioRunner
from carlabridge.scenarios.s1_fire import EXTINGUISH_DWELL_S, S1FireScenario
from carlabridge.sensors.camera import CameraBinding, CameraManager, CameraSpec
from tests.test_camera_manager import FakeSpawner


# ---- CARLA-shaped fakes (mirror test_reset_reinit) -----------------------


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

    def get_map(self): return _Map()
    def get_blueprint_library(self): return _BpLib()
    def try_spawn_actor(self, _bp, _tf) -> _Actor:
        self._next += 1
        return _Actor(self._next)


@dataclass
class _Facade:
    carla_world: Any


# ---- fixture -------------------------------------------------------------


@pytest.fixture
async def live_bridge_with_ticker():
    """Live HTTP + Socket.IO + ticker thread driving the scenario lifecycle."""
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
        bridge_session_id="br-instant",
        scenario_name="s1_fire",
    )
    agent_ns = app["agent_ns"]

    # Sim → async hop for command_status broadcasts (mirrors main.py).
    def _on_status(payload):
        loop.call_soon_threadsafe(
            sio.start_background_task,
            agent_ns.broadcast_command_status,
            payload,
        )

    def _on_event(payload):
        loop.call_soon_threadsafe(
            sio.start_background_task,
            agent_ns.broadcast_scenario_event,
            payload,
        )

    bus.set_on_command_status(_on_status)
    bus.set_on_scenario_event(_on_event)

    runner = ScenarioRunner(
        S1FireScenario,
        world=_Facade(_CarlaWorld()),
        fleet=Fleet(),
        camera_manager=cam,
        event_log=event_log,
        command_bus=bus,
        sim_time_provider=lambda: 0.0,
    )
    runner.start()
    app["late"]["scenario_runner"] = runner
    agent_ns.set_resetting_provider(runner.is_resetting)

    # Ticker simulating the real tick loop @ ~100 Hz.
    stop = threading.Event()
    sim_time = [0.0]

    def _ticker_loop():
        while not stop.is_set():
            # 1. drain bus → scenario.on_command (rejections swallowed)
            scenario = runner.scenario
            if scenario is not None:
                for cmd in bus.drain():
                    try:
                        scenario.on_command(cmd)
                    except Exception:
                        pass
                # 2. drain HTTP sim_tasks
                runner.drain_sim_tasks()
                # 3. tick (drives _drive_command_lifecycle)
                try:
                    scenario.on_tick_post(sim_time[0])
                except Exception:
                    pass
            sim_time[0] += 0.01
            stop.wait(0.01)

    ticker = threading.Thread(target=_ticker_loop, daemon=True)
    ticker.start()

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, runner
    finally:
        stop.set()
        ticker.join(timeout=1.0)
        await client.close()
        try:
            runner.stop()
        except Exception:
            pass


# ---- helpers -------------------------------------------------------------


async def _connect(client: TestClient) -> tuple[socketio.AsyncClient, list[dict]]:
    """Connect a Socket.IO client to /agent and tap command_status events."""
    sio_client = socketio.AsyncClient()
    received: list[dict] = []

    @sio_client.on("command_status", namespace="/agent")
    def _on_cs(payload):
        received.append(payload)

    await sio_client.connect(
        f"http://127.0.0.1:{client.server.port}", namespaces=["/agent"],
    )
    return sio_client, received


async def _wait_for_status(received: list[dict], cmd_id: str, status: str,
                           timeout: float = 1.0) -> dict:
    """Poll the captured event list until matching status arrives."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for p in received:
            if p["cmd_id"] == cmd_id and p["status"] == status:
                return p
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"no command_status:{status} for {cmd_id} within {timeout}s "
        f"(received={[(p['cmd_id'], p['status']) for p in received]})"
    )


# ---- HOLD ---------------------------------------------------------------


async def test_uav_hold_completes_within_one_tick(live_bridge_with_ticker):
    client, _ = live_bridge_with_ticker
    sio_client, received = await _connect(client)
    try:
        ack = await sio_client.call(
            "agent.command",
            {"id": "cmd-hold", "kind": "UAV_HOLD", "target": "UAV-01"},
            namespace="/agent", timeout=2,
        )
        assert ack["status"] == "accepted"
        assert ack["cmd_id"] == "cmd-hold"
        completed = await _wait_for_status(received, "cmd-hold", "completed")
        assert completed["kind"] == "UAV_HOLD"
        assert completed["target"] == "UAV-01"
        assert completed["reason"] is None
    finally:
        await sio_client.disconnect()


# ---- STOP ---------------------------------------------------------------


async def test_ugv_stop_completes_within_one_tick(live_bridge_with_ticker):
    client, _ = live_bridge_with_ticker
    sio_client, received = await _connect(client)
    try:
        ack = await sio_client.call(
            "agent.command",
            {"id": "cmd-stop", "kind": "UGV_STOP", "target": "UGV-01"},
            namespace="/agent", timeout=2,
        )
        assert ack["status"] == "accepted"
        completed = await _wait_for_status(received, "cmd-stop", "completed")
        assert completed["kind"] == "UGV_STOP"
    finally:
        await sio_client.disconnect()


# ---- EXTINGUISH ---------------------------------------------------------


async def test_ugv_extinguish_completes_after_dwell_and_clears_incident(
    live_bridge_with_ticker,
):
    client, runner = live_bridge_with_ticker
    # Pre-stage an incident close to the UGV (which sits at origin (0,0,0)).
    fleet = runner.scenario.fleet
    fake_fire_actor = _Actor(idv=9999)
    runner.scenario._fire_actors["fire-001"] = fake_fire_actor
    fleet.add_incident(Incident(
        id="fire-001", kind="fire", position=Pose(x=2.0, y=0.0, z=0.0),
        severity="high", since_sim_time=0.0,
    ))

    sio_client, received = await _connect(client)
    try:
        ack = await sio_client.call(
            "agent.command",
            {"id": "cmd-ext", "kind": "UGV_EXTINGUISH", "target": "UGV-01",
             "params": {"incident_id": "fire-001"}},
            namespace="/agent", timeout=2,
        )
        assert ack["status"] == "accepted"
        completed = await _wait_for_status(
            received, "cmd-ext", "completed",
            timeout=max(4.0, EXTINGUISH_DWELL_S + 2.0),
        )
        assert completed["kind"] == "UGV_EXTINGUISH"
        # Fire actor destroyed + incident cleared as part of completion.
        assert fake_fire_actor.destroyed
        assert fleet.get_incident("fire-001") is None
    finally:
        await sio_client.disconnect()


async def test_two_distinct_entity_instants_both_complete(live_bridge_with_ticker):
    """UAV_HOLD completes quickly; UGV_EXTINGUISH completes after dwell.
    (Same-entity back-to-back would supersede; that's covered by test_supersede.)"""
    client, runner = live_bridge_with_ticker
    fleet = runner.scenario.fleet
    fake_fire = _Actor(idv=7777)
    runner.scenario._fire_actors["fire-x"] = fake_fire
    fleet.add_incident(Incident(
        id="fire-x", kind="fire", position=Pose(x=1.0, y=0.0, z=0.0),
        severity="high", since_sim_time=0.0,
    ))

    sio_client, received = await _connect(client)
    try:
        ack_h = await sio_client.call(
            "agent.command",
            {"id": "h-1", "kind": "UAV_HOLD", "target": "UAV-02"},
            namespace="/agent", timeout=2,
        )
        assert ack_h["status"] == "accepted"
        ack_e = await sio_client.call(
            "agent.command",
            {"id": "e-1", "kind": "UGV_EXTINGUISH", "target": "UGV-01",
             "params": {"incident_id": "fire-x"}},
            namespace="/agent", timeout=2,
        )
        assert ack_e["status"] == "accepted"
        await _wait_for_status(received, "h-1", "completed", timeout=2.0)
        await _wait_for_status(
            received, "e-1", "completed",
            timeout=max(4.0, EXTINGUISH_DWELL_S + 2.0),
        )
        assert fake_fire.destroyed
        assert fleet.get_incident("fire-x") is None
    finally:
        await sio_client.disconnect()
