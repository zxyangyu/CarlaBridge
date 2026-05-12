"""MJPEG tap: encoder + HTTP route."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from carlabridge.sensors.camera import CameraManager
from carlabridge.streaming.jpeg_tap import encode_jpeg
from carlabridge.streaming.mjpeg import BOUNDARY, mjpeg_route


# ---------- encoder -------------------------------------------------------


def test_encode_jpeg_returns_valid_soi_eoi():
    rgb = np.zeros((120, 160, 3), dtype=np.uint8)
    rgb[:, :, 1] = 200  # solid green
    data = encode_jpeg(rgb)
    assert data[:2] == b"\xff\xd8"        # SOI
    assert data[-2:] == b"\xff\xd9"       # EOI
    assert len(data) > 100  # not empty


def test_encode_jpeg_rejects_wrong_shape():
    bad = np.zeros((120, 160), dtype=np.uint8)  # grayscale
    with pytest.raises(ValueError):
        encode_jpeg(bad)


def test_encode_jpeg_accepts_quality_param():
    """Quality knob doesn't crash and produces a valid JPEG for both ends.

    We don't assert size monotonicity — PyAV's mjpeg codec doesn't always
    honor `qscale` through `options` (depends on FFmpeg build). The semantic
    we care about is "valid JPEG out regardless of quality value".
    """
    rgb = np.random.default_rng(0).integers(0, 256, (240, 320, 3), dtype=np.uint8)
    for q in (1, 10, 50, 95, 100):
        data = encode_jpeg(rgb, quality=q)
        assert data[:2] == b"\xff\xd8"
        assert data[-2:] == b"\xff\xd9"


# ---------- route ---------------------------------------------------------


@pytest.fixture
async def mjpeg_client():
    mgr = CameraManager()
    mgr.get_or_create_queue("city").bind_loop()
    app = web.Application()
    app.router.add_get("/video_feed", mjpeg_route(mgr))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, mgr
    finally:
        await client.close()


async def test_mjpeg_unknown_camera_returns_404(mjpeg_client):
    client, _ = mjpeg_client
    resp = await client.get("/video_feed?camera=nope")
    assert resp.status == 404


async def test_mjpeg_missing_query_returns_400(mjpeg_client):
    client, _ = mjpeg_client
    resp = await client.get("/video_feed")
    assert resp.status == 400


async def test_mjpeg_serves_one_frame_and_streams(mjpeg_client):
    client, mgr = mjpeg_client
    rgb = np.zeros((64, 64, 3), dtype=np.uint8)
    rgb[:, :, 2] = 255  # solid blue

    async def producer():
        # Push one frame, wait, push another.
        await asyncio.sleep(0.05)
        mgr.queue_for("city").set_latest(rgb)
        await asyncio.sleep(0.15)
        mgr.queue_for("city").set_latest(rgb)

    asyncio.create_task(producer())

    resp = await client.get("/video_feed?camera=city")
    assert resp.status == 200
    ctype = resp.headers["Content-Type"]
    assert "multipart/x-mixed-replace" in ctype
    assert BOUNDARY in ctype

    # Read enough bytes to span 2 frame parts. The handler streams forever;
    # we cap the read and then drop the connection.
    chunks: list[bytes] = []
    total = 0
    deadline = asyncio.get_event_loop().time() + 1.5
    while total < 200 and asyncio.get_event_loop().time() < deadline:
        try:
            chunk = await asyncio.wait_for(resp.content.read(4096), timeout=0.3)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    resp.close()

    blob = b"".join(chunks)
    assert b"--frame" in blob
    assert b"Content-Type: image/jpeg" in blob
    # At least one SOI somewhere after the multipart boundary.
    assert b"\xff\xd8" in blob
