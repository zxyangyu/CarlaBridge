"""End-to-end: POST /scenario/reset cancels every in-flight command (R8-05).

Spin a live HTTP + Socket.IO bridge. Push 2 UAV_GOTO + 1 UGV_GOTO through
``sio.call('agent.command', ...)`` so they sit in-flight (long-running
commands with distant targets). POST /scenario/reset and assert three
``command_status: cancelled(reason="reset")`` events fire — one per cmd_id.
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
from carlabridge.core.fleet import Fleet
from carlabridge.core.snapshot import WorldSnapshot
from carlabridge.obs.event_log import EventLog
from carlabridge.obs.metrics import Metrics
from carlabridge.scenarios.runner import ScenarioRunner
from carlabridge.scenarios.s1_fire import S1FireScenario
from carlabridge.sensors.camera import CameraBinding, CameraManager, CameraSpec
from tests.test_camera_manager import FakeSpawner


# ---- CARLA-shaped fakes ---------------------------------------------------


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


# ---- fixture --------------------------------------------------------------


@pytest.fixture
async def live_bridge_with_ticker():
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
        bridge_session_id="br-reset",
        scenario_name="s1_fire",
    )
    agent_ns = app["agent_ns"]

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

    # Replace the SimpleWaypointFollower wiring with a no-op fake so UGV_GOTO
    # doesn't crash against the FakeCarlaWorld (no GlobalRoutePlanner map).
    class _FakeFollower:
        def __init__(self):
            self._done = False

        def set_destination(self, _world, _loc):
            pass

        def run_step(self):
            return None  # apply_control accepts any object on FakeActor

        def done(self) -> bool:
            return self._done

    scenario = runner.scenario
    scenario._make_follower = lambda _actor, _mps: _FakeFollower()  # type: ignore[attr-defined]
    scenario._set_destination = lambda _f, _dest: None  # type: ignore[attr-defined]

    stop = threading.Event()

    def _ticker_loop():
        sim_time = [0.0]
        while not stop.is_set():
            scenario = runner.scenario
            if scenario is not None:
                for cmd in bus.drain():
                    try:
                        scenario.on_command(cmd)
                    except Exception:
                        pass
                runner.drain_sim_tasks()
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


def _url(client: TestClient) -> str:
    return f"http://127.0.0.1:{client.server.port}"


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"predicate stayed False for {timeout}s")


# ---- the test ------------------------------------------------------------


async def test_reset_cancels_two_uav_goto_and_one_ugv_goto(live_bridge_with_ticker):
    client, runner = live_bridge_with_ticker

    sio_client = socketio.AsyncClient()
    received: list[dict] = []

    @sio_client.on("command_status", namespace="/agent")
    def _on_cs(payload):
        received.append(payload)

    await sio_client.connect(_url(client), namespaces=["/agent"])
    try:
        # 1. Fire 2 UAV_GOTOs + 1 UGV_GOTO with distant targets so none of
        # them auto-complete before /reset.
        cmds = [
            ("cmd-uav-1", "UAV_GOTO", "UAV-01",
             {"waypoint": {"x": 10000.0, "y": 0.0, "z": 60.0},
              "cruise_speed": 0.1}),
            ("cmd-uav-2", "UAV_GOTO", "UAV-02",
             {"waypoint": {"x": -10000.0, "y": 0.0, "z": 60.0},
              "cruise_speed": 0.1}),
            ("cmd-ugv", "UGV_GOTO", "UGV-01",
             {"dest": {"x": 10000.0, "y": 0.0, "z": 0.0},
              "target_speed": 1.0}),
        ]
        for cid, kind, target, params in cmds:
            ack = await sio_client.call(
                "agent.command",
                {"id": cid, "kind": kind, "target": target, "params": params},
                namespace="/agent", timeout=2,
            )
            assert ack["status"] == "accepted", f"{cid} not accepted: {ack}"

        # 2. Wait until all 3 are registered in-flight (ticker has drained).
        await _wait_until(
            lambda: len(runner.scenario._in_flight) == 3,
            timeout=2.0,
        )

        # 3. POST /scenario/reset.
        async with client.post("/scenario/reset", json={}) as resp:
            assert resp.status == 200
            body = await resp.json()
        assert body["status"] == "ok"
        assert set(body["cancelled_commands"]) == {"cmd-uav-1", "cmd-uav-2", "cmd-ugv"}
        assert body["run_id"] == 1

        # 4. Three cancelled(reason=reset) events arrived for our cmd ids.
        await _wait_until(
            lambda: {
                p["cmd_id"] for p in received
                if p["status"] == "cancelled" and p["reason"] == "reset"
            } >= {"cmd-uav-1", "cmd-uav-2", "cmd-ugv"},
            timeout=2.0,
        )
        cancellations = [
            p for p in received
            if p["status"] == "cancelled" and p["reason"] == "reset"
        ]
        by_id = {p["cmd_id"]: p for p in cancellations}
        assert set(by_id.keys()) == {"cmd-uav-1", "cmd-uav-2", "cmd-ugv"}
        for p in cancellations:
            assert p["detail"] == {"trigger": "http"}

        # 5. In-flight cleared after reset.
        assert runner.scenario._in_flight == {}
    finally:
        await sio_client.disconnect()


async def test_reset_also_emits_scenario_event(live_bridge_with_ticker):
    client, runner = live_bridge_with_ticker
    sio_client = socketio.AsyncClient()
    scenario_events: list[dict] = []

    @sio_client.on("scenario_event", namespace="/agent")
    def _on_se(payload):
        scenario_events.append(payload)

    await sio_client.connect(_url(client), namespaces=["/agent"])
    try:
        async with client.post("/scenario/reset", json={}) as resp:
            assert resp.status == 200
        await _wait_until(
            lambda: any(e.get("event") == "reset" for e in scenario_events),
            timeout=2.0,
        )
        evt = next(e for e in scenario_events if e.get("event") == "reset")
        assert evt["run_id"] == 1
        assert evt["trigger"] == "http"
    finally:
        await sio_client.disconnect()
