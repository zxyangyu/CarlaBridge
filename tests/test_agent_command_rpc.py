"""End-to-end: agent.command via ``sio.call`` (R5-01)."""

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


# ---- fixture -------------------------------------------------------------


@pytest.fixture
async def live_bridge():
    """Spin up a real aiohttp + Socket.IO server bound to a free port and
    yield (port, command_bus). Caller connects an AsyncClient and tests RPC.

    Cleanup tears the server down before yielding control back so per-test
    state is hermetic.
    """
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
        bridge_session_id="br-testfix",
        scenario_name="s1_fire",
    )
    server = TestServer(app)
    await server.start_server()
    try:
        yield server, bus
    finally:
        await server.close()


def _url(server: TestServer) -> str:
    return f"http://127.0.0.1:{server.port}"


# ---- accepted path -------------------------------------------------------


async def test_agent_command_accepted_returns_dict(live_bridge):
    server, _ = live_bridge
    client = socketio.AsyncClient()
    try:
        await client.connect(_url(server), namespaces=["/agent"])
        ack = await client.call(
            "agent.command",
            {
                "id": "cmd-001",
                "kind": "UAV_HOLD",
                "target": "UAV-01",
            },
            namespace="/agent",
            timeout=2,
        )
        assert ack["status"] == "accepted"
        assert ack["cmd_id"] == "cmd-001"
        assert "queued_at_sim_time" in ack
    finally:
        await client.disconnect()


async def test_agent_command_envelope_wrapping_accepted(live_bridge):
    """Envelope ``{... "payload": {cmd}}`` flavor also accepted (design §3.1)."""
    server, _ = live_bridge
    client = socketio.AsyncClient()
    try:
        await client.connect(_url(server), namespaces=["/agent"])
        ack = await client.call(
            "agent.command",
            {
                "version": "1.0",
                "type": "agent.command",
                "payload": {
                    "id": "cmd-002",
                    "kind": "UAV_HOLD",
                    "target": "UAV-01",
                },
            },
            namespace="/agent",
            timeout=2,
        )
        assert ack["status"] == "accepted"
        assert ack["cmd_id"] == "cmd-002"
    finally:
        await client.disconnect()


# ---- rejected paths ------------------------------------------------------


async def test_agent_command_parse_error_returns_rejected(live_bridge):
    server, _ = live_bridge
    client = socketio.AsyncClient()
    try:
        await client.connect(_url(server), namespaces=["/agent"])
        ack = await client.call(
            "agent.command",
            {"id": "cmd-003", "kind": "NUKE_FROM_ORBIT", "target": "UAV-01"},
            namespace="/agent",
            timeout=2,
        )
        assert ack["status"] == "rejected"
        assert ack["cmd_id"] == "cmd-003"
        assert ack["reason"] == "parse_error"
        assert isinstance(ack["detail"], dict)
        assert ack["detail"].get("field") == "kind"
    finally:
        await client.disconnect()


async def test_agent_command_kind_target_mismatch_rejected(live_bridge):
    server, _ = live_bridge
    client = socketio.AsyncClient()
    try:
        await client.connect(_url(server), namespaces=["/agent"])
        ack = await client.call(
            "agent.command",
            {"id": "cmd-004", "kind": "UAV_HOLD", "target": "UGV-01"},
            namespace="/agent",
            timeout=2,
        )
        assert ack["status"] == "rejected"
        assert ack["reason"] == "kind_target_mismatch"
        assert ack["detail"]["kind"] == "UAV_HOLD"
        assert ack["detail"]["target"] == "UGV-01"
    finally:
        await client.disconnect()


async def test_agent_command_overloaded_returns_rejected(live_bridge):
    """Bus full → sio.call returns overloaded reason."""
    server, bus = live_bridge
    # Pre-fill the bus to capacity.
    from carlabridge.commands.enum import CommandKind, ParsedCommand
    for i in range(64):
        bus.submit(ParsedCommand(id=f"pre-{i}", kind=CommandKind.UAV_HOLD, target="UAV-01"))

    client = socketio.AsyncClient()
    try:
        await client.connect(_url(server), namespaces=["/agent"])
        ack = await client.call(
            "agent.command",
            {"id": "cmd-005", "kind": "UAV_HOLD", "target": "UAV-01"},
            namespace="/agent",
            timeout=2,
        )
        assert ack["status"] == "rejected"
        assert ack["reason"] == "overloaded"
        assert ack["cmd_id"] == "cmd-005"
    finally:
        await client.disconnect()


# ---- hello handshake (R5-03) --------------------------------------------


async def test_hello_returns_bridge_session_id(live_bridge):
    server, _ = live_bridge
    client = socketio.AsyncClient()
    try:
        await client.connect(_url(server), namespaces=["/agent"])
        ack = await client.call(
            "hello", {"agent_id": "test-agent"},
            namespace="/agent", timeout=2,
        )
        assert ack["server"] == "carlabridge"
        # Protocol v1.0 §2.2: hello return must carry the protocol version.
        assert ack["version"] == "1.0"
        assert ack["bridge_session_id"] == "br-testfix"
        assert ack["scenario"] == "s1_fire"
    finally:
        await client.disconnect()
