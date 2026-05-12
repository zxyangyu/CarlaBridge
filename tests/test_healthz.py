"""Smoke /healthz: required fields present, defaults reasonable."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from carlabridge.bus.projector import FocusBinding
from carlabridge.bus.server import build_app, make_sio
from carlabridge.commands.bus import CommandBus
from carlabridge.config import Settings
from carlabridge.core.atomic import AtomicRef
from carlabridge.core.snapshot import WorldSnapshot
from carlabridge.obs.event_log import EventLog
from carlabridge.obs.metrics import Metrics
from carlabridge.sensors.camera import CameraBinding, CameraManager, CameraSpec


@pytest.fixture
async def healthz_client():
    settings = Settings()
    event_log = EventLog(capacity=50)
    metrics = Metrics()
    metrics.set("tick_fps", 29.5)
    cam_mgr = CameraManager()
    cam_mgr.bind(CameraBinding(spec=CameraSpec(id="city", mode="world_pose")))
    cam_mgr.bind(CameraBinding(spec=CameraSpec(id="aerial", mode="follows_virtual")))
    sio = make_sio(settings)
    import asyncio
    bus = CommandBus(loop=asyncio.get_event_loop(), sio=sio, event_log=event_log)
    app, _ = build_app(
        settings, event_log, metrics,
        sio=sio,
        snapshot_ref=AtomicRef[WorldSnapshot](),
        focus=FocusBinding(),
        camera_manager=cam_mgr,
        command_bus=bus,
    )
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def test_healthz_returns_required_fields(healthz_client):
    resp = await healthz_client.get("/healthz")
    assert resp.status == 200
    body = await resp.json()
    # design §15.4 mandatory fields
    assert body["status"] == "alive"
    assert "carla" in body
    assert "tick_fps" in body
    assert "scenario" in body
    assert "clients" in body
    assert "cameras" in body
    # spec details
    assert body["clients"]["frontend"] == 0
    assert body["clients"]["agent"] == 0
    assert "city" in body["cameras"]
    assert body["cameras"]["city"]["status"] == "unbound"  # no spawn
    assert body["tick_fps"] == 29.5  # from metrics


async def test_healthz_carla_disconnected_when_no_snapshot(healthz_client):
    body = await (await healthz_client.get("/healthz")).json()
    # Snapshot ref is None → not connected.
    assert body["carla"] == "disconnected"


async def test_healthz_scenario_state_none_when_no_runner(healthz_client):
    body = await (await healthz_client.get("/healthz")).json()
    assert body["scenario"] == "none/idle"


async def test_healthz_command_bus_depth(healthz_client):
    body = await (await healthz_client.get("/healthz")).json()
    assert body["command_bus"]["depth"] == 0
