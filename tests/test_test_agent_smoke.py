"""End-to-end smoke for the repo-root ``test_agent.py`` (R9-02 DoD).

Rather than spawning the script as a subprocess (which couples this test to
process lifecycle), we import the :class:`TestAgent` class directly and
drive it against a live Bridge fixture with a ticker thread. That exercises
every code path in the script except :func:`main` plumbing.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import socketio
from aiohttp.test_utils import TestClient, TestServer

from carlabridge.bus.broadcaster import Broadcaster
from carlabridge.bus.projector import FocusBinding
from carlabridge.bus.server import build_app, make_sio
from carlabridge.commands.bus import CommandBus
from carlabridge.config import FireMarkerCfg
from tests.spawn_config import make_spawn_settings
from carlabridge.core.atomic import AtomicRef
from carlabridge.core.fleet import Fleet
from carlabridge.core.snapshot import SnapshotBuilder, WorldSnapshot
from carlabridge.obs.event_log import EventLog
from carlabridge.obs.metrics import Metrics
from carlabridge.scenarios.runner import ScenarioRunner
from carlabridge.scenarios.s1_fire import EXTINGUISH_DWELL_S, S1FireScenario
from carlabridge.sensors.camera import CameraBinding, CameraManager, CameraSpec
from tests.test_camera_manager import FakeSpawner


# ---- import test_agent.py from repo root ---------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENT_PATH = _REPO_ROOT / "test_agent.py"


def _import_test_agent_module():
    spec = importlib.util.spec_from_file_location("repo_test_agent", _AGENT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["repo_test_agent"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---- CARLA-shaped fakes --------------------------------------------------


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


# ---- live bridge with snapshot broadcaster (so the client sees snapshots) -


@pytest.fixture
async def live_bridge_full():
    settings = make_spawn_settings(
        fire_markers=[FireMarkerCfg(id="fire-001", x=1.0, y=0.0, z=0.0)],
    )
    settings.broadcast.state_hz = 20.0  # faster for tests
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
    snapshot_ref: AtomicRef[WorldSnapshot] = AtomicRef()
    focus = FocusBinding()

    app, _ = build_app(
        settings, event_log, metrics,
        sio=sio,
        snapshot_ref=snapshot_ref,
        focus=focus,
        camera_manager=cam,
        command_bus=bus,
        bridge_session_id="br-r9smoke",
        scenario_name="s1_fire",
    )
    agent_ns = app["agent_ns"]

    def _on_status(payload):
        loop.call_soon_threadsafe(
            sio.start_background_task,
            agent_ns.broadcast_command_status, payload,
        )

    def _on_event(payload):
        loop.call_soon_threadsafe(
            sio.start_background_task,
            agent_ns.broadcast_scenario_event, payload,
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
        settings=settings,
    )
    runner.start()
    app["late"]["scenario_runner"] = runner
    agent_ns.set_resetting_provider(runner.is_resetting)
    agent_ns.set_sim_time_provider(runner.sim_time)

    # Fake follower so UGV_GOTO/RTL don't hit the real GlobalRoutePlanner.
    class _FakeFollower:
        def set_destination(self, *a, **kw): pass
        def run_step(self): return None
        def done(self): return False

    scen = runner.scenario
    scen._make_follower = lambda _a, _m: _FakeFollower()  # type: ignore[attr-defined]
    scen._set_destination = lambda _f, _d: None           # type: ignore[attr-defined]

    # Snapshot builder + tick simulator.
    snap_builder = SnapshotBuilder(world=None)
    stop = threading.Event()
    sim_t = [0.0]

    def _ticker():
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
                    scenario.on_tick_post(sim_t[0])
                except Exception:
                    pass
                # Rebuild snapshot so the broadcaster has fresh state to push.
                try:
                    snap = snap_builder.build(
                        runner._fleet, sim_t[0],
                        run_id=int(getattr(scenario, "_run_id", 0)),
                        bridge_session_id=agent_ns.bridge_session_id,
                        in_flight_commands=scenario.in_flight_snapshot(),
                    )
                    snapshot_ref.set(snap)
                except Exception:
                    pass
            sim_t[0] += 0.01
            stop.wait(0.01)

    ticker = threading.Thread(target=_ticker, daemon=True)
    ticker.start()

    broadcaster = Broadcaster(
        sio=sio, snapshot_ref=snapshot_ref, focus=focus,
        metrics=metrics, event_log=event_log,
        state_hz=settings.broadcast.state_hz,
        metrics_hz=settings.broadcast.metrics_hz,
    )
    broadcaster.start()

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, runner
    finally:
        await broadcaster.stop()
        stop.set()
        ticker.join(timeout=1.0)
        await client.close()
        try:
            runner.stop()
        except Exception:
            pass


async def _wait(predicate, timeout: float = 8.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"predicate stayed False for {timeout}s")


# ---- the smoke flow ------------------------------------------------------


async def test_test_agent_patrol_then_fire_then_extinguish(live_bridge_full):
    """End-to-end §8.1 flow: connect → PATROL × 3 → fire → UGV_GOTO →
    range check → UGV_EXTINGUISH → completed → UGV_RTL."""
    client, runner = live_bridge_full
    mod = _import_test_agent_module()

    sio = socketio.AsyncClient()
    agent = mod.TestAgent(sio, no_extinguish=False, verbose=False)
    sio.on("state_snapshot", agent.on_snapshot, namespace=mod.NAMESPACE)
    sio.on("command_status", agent.on_command_status, namespace=mod.NAMESPACE)
    sio.on("scenario_event", agent.on_scenario_event, namespace=mod.NAMESPACE)
    sio.on("event_log", agent.on_event_log, namespace=mod.NAMESPACE)

    url = f"http://127.0.0.1:{client.server.port}"
    await sio.connect(url, namespaces=[mod.NAMESPACE])
    # hello returns session id.
    hello_ack = await sio.call("hello", {"agent_id": "test-agent"},
                                namespace=mod.NAMESPACE, timeout=2.0)
    assert hello_ack["bridge_session_id"] == "br-r9smoke"
    agent.bridge_session_id = hello_ack["bridge_session_id"]
    try:
        # 1. PATROL × 3 lands after first snapshot.
        def patrolled():
            kinds = {f.kind.value for f in runner.scenario._in_flight.values()}
            return kinds == {"UAV_PATROL"} and len(runner.scenario._in_flight) == 1
        await _wait(patrolled, timeout=8.0)

        # 2. Ignite a fire close to the UGV (UGV is at (0,0,0)).
        async with client.post("/scenario/fire", json={
            "id": "fire-001",
            "position": {"x": 1.0, "y": 0.0, "z": 0.0},
        }) as resp:
            assert resp.status == 200

        # Pre-position a fake fire actor so EXTINGUISH can "destroy" it.
        from tests.test_test_agent_smoke import _Actor as ActorFake  # self-import
        scenario = runner.scenario
        if "fire-001" not in scenario._fire_actors:
            scenario._fire_actors["fire-001"] = ActorFake(idv=9001)

        # 3. test_agent sends UGV_GOTO (state = going).
        await _wait(lambda: "fire-001" in agent.responding, timeout=8.0)
        await _wait(
            lambda: any(
                f.kind.value == "UGV_GOTO" and f.target == "UGV-01"
                for f in runner.scenario._in_flight.values()
            ),
            timeout=8.0,
        )

        # 4. UGV is within range (distance from (0,0,0) to (1,0,0) is 1m,
        # well under 5m), so test_agent should escalate to UGV_EXTINGUISH.
        await _wait(
            lambda: agent.responding.get("fire-001") == "extinguishing"
            or "fire-001" not in agent.responding,  # already cleared after complete
            timeout=8.0,
        )

        # 5. After dwell + completion, incident gone from fleet & agent sends RTL.
        extinguish_budget = max(8.0, EXTINGUISH_DWELL_S + 4.0)
        await _wait(
            lambda: runner.scenario.fleet.get_incident("fire-001") is None,
            timeout=extinguish_budget,
        )
        await _wait(
            lambda: any(
                f.kind.value == "UGV_RTL" and f.target == "UGV-01"
                for f in runner.scenario._in_flight.values()
            ),
            timeout=8.0,
        )
    finally:
        await sio.disconnect()


async def test_test_agent_replays_patrol_after_reset(live_bridge_full):
    """scenario_event(reset) → state cleared → next snapshot triggers PATROL again."""
    client, runner = live_bridge_full
    mod = _import_test_agent_module()

    sio = socketio.AsyncClient()
    agent = mod.TestAgent(sio, no_extinguish=False, verbose=False)
    sio.on("state_snapshot", agent.on_snapshot, namespace=mod.NAMESPACE)
    sio.on("command_status", agent.on_command_status, namespace=mod.NAMESPACE)
    sio.on("scenario_event", agent.on_scenario_event, namespace=mod.NAMESPACE)
    sio.on("event_log", agent.on_event_log, namespace=mod.NAMESPACE)
    url = f"http://127.0.0.1:{client.server.port}"
    await sio.connect(url, namespaces=[mod.NAMESPACE])
    try:
        # First PATROL cycle.
        await _wait(
            lambda: sum(
                1 for f in runner.scenario._in_flight.values()
                if f.kind.value == "UAV_PATROL"
            ) == 1,
            timeout=3.0,
        )

        # Trigger reset. Capture run_id at the time the agent observes it so
        # we can verify the agent picked up scenario_event (run_id flip).
        pre_run = agent.run_id

        async with client.post("/scenario/reset", json={}) as resp:
            assert resp.status == 200

        # Agent's run_id must advance once scenario_event arrives + a fresh
        # snapshot lands.
        await _wait(
            lambda: agent.run_id is not None and agent.run_id != pre_run,
            timeout=3.0,
        )

        # Re-PATROL must happen on the post-reset snapshot.
        await _wait(
            lambda: sum(
                1 for f in runner.scenario._in_flight.values()
                if f.kind.value == "UAV_PATROL"
            ) == 1,
            timeout=3.0,
        )
    finally:
        await sio.disconnect()
