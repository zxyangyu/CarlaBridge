"""End-to-end: agent_ns broadcast helpers (R5-02)."""

from __future__ import annotations

import asyncio

import pytest
import socketio
from aiohttp.test_utils import TestServer

from carlabridge.bus.projector import FocusBinding
from carlabridge.bus.server import build_app, make_sio
from carlabridge.commands.bus import CommandBus
from carlabridge.config import Settings
from carlabridge.core.atomic import AtomicRef
from carlabridge.core.snapshot import WorldSnapshot
from carlabridge.obs.event_log import EventLog
from carlabridge.obs.metrics import Metrics
from carlabridge.sensors.camera import CameraManager


@pytest.fixture
async def live_bridge():
    settings = Settings()
    event_log = EventLog(capacity=200)
    metrics = Metrics()
    sio_server = make_sio(settings)
    loop = asyncio.get_event_loop()
    bus = CommandBus(loop=loop, sio=sio_server, event_log=event_log)
    app, _ = build_app(
        settings, event_log, metrics,
        sio=sio_server,
        snapshot_ref=AtomicRef[WorldSnapshot](),
        focus=FocusBinding(),
        camera_manager=CameraManager(),
        command_bus=bus,
        bridge_session_id="br-broadcast",
        scenario_name="s1_fire",
    )
    agent_ns = app["agent_ns"]

    # Mirror main's sim→async hop so the bus callbacks reach the namespace.
    def _on_status(payload):
        loop.call_soon_threadsafe(
            sio_server.start_background_task,
            agent_ns.broadcast_command_status,
            payload,
        )

    def _on_event(payload):
        loop.call_soon_threadsafe(
            sio_server.start_background_task,
            agent_ns.broadcast_scenario_event,
            payload,
        )

    bus.set_on_command_status(_on_status)
    bus.set_on_scenario_event(_on_event)

    server = TestServer(app)
    await server.start_server()
    try:
        yield server, bus, agent_ns
    finally:
        await server.close()


def _url(server: TestServer) -> str:
    return f"http://127.0.0.1:{server.port}"


# ---- command_status broadcast --------------------------------------------


async def test_broadcast_command_status_reaches_subscribed_client(live_bridge):
    server, bus, _ = live_bridge
    received: list[dict] = []
    done = asyncio.Event()

    client = socketio.AsyncClient()

    @client.on("command_status", namespace="/agent")
    def _on_cs(payload):
        received.append(payload)
        done.set()

    try:
        await client.connect(_url(server), namespaces=["/agent"])
        # Fire from "sim domain" via the bus.
        bus.broadcast_command_status({
            "cmd_id": "cmd-a", "status": "completed",
            "kind": "UAV_HOLD", "target": "UAV-01",
            "reason": None, "detail": None, "at_sim_time": 1.0,
        })
        await asyncio.wait_for(done.wait(), timeout=2.0)
    finally:
        await client.disconnect()

    assert len(received) == 1
    env = received[0]
    # Protocol v1.0 §3.1 envelope wraps the command_status payload.
    assert env["version"] == "1.0"
    assert env["type"] == "command_status"
    assert env["sender"] == "bridge"
    assert env["sim_time"] == 1.0
    p = env["payload"]
    assert p["cmd_id"] == "cmd-a"
    assert p["status"] == "completed"


async def test_broadcast_command_status_fan_out_to_multiple_clients(live_bridge):
    server, bus, _ = live_bridge
    counts = [0, 0]
    barrier = asyncio.Event()

    async def _start(client, idx):
        @client.on("command_status", namespace="/agent")
        def _on_cs(_payload):
            counts[idx] += 1
            if counts[0] >= 1 and counts[1] >= 1:
                barrier.set()
        await client.connect(_url(server), namespaces=["/agent"])

    c1, c2 = socketio.AsyncClient(), socketio.AsyncClient()
    try:
        await _start(c1, 0)
        await _start(c2, 1)
        bus.broadcast_command_status({
            "cmd_id": "cmd-fan", "status": "completed",
            "kind": "UAV_HOLD", "target": "UAV-01",
            "reason": None, "detail": None, "at_sim_time": 0.0,
        })
        await asyncio.wait_for(barrier.wait(), timeout=2.0)
    finally:
        await c1.disconnect()
        await c2.disconnect()
    assert counts == [1, 1]


# ---- scenario_event broadcast --------------------------------------------


async def test_broadcast_scenario_event_reset_reaches_client(live_bridge):
    server, bus, _ = live_bridge
    received: list[dict] = []
    done = asyncio.Event()

    client = socketio.AsyncClient()

    @client.on("scenario_event", namespace="/agent")
    def _on_se(payload):
        received.append(payload)
        done.set()

    try:
        await client.connect(_url(server), namespaces=["/agent"])
        bus.broadcast_scenario_event({
            "event": "reset", "run_id": 7, "trigger": "http",
        })
        await asyncio.wait_for(done.wait(), timeout=2.0)
    finally:
        await client.disconnect()

    env = received[0]
    assert env["version"] == "1.0"
    assert env["type"] == "scenario_event"
    p = env["payload"]
    assert p["event"] == "reset"
    assert p["run_id"] == 7
