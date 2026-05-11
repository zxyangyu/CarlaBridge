"""End-to-end WebRTC signaling: local RTCPeerConnection ↔ /webrtc/{camera_id}.

We don't validate media flow here (that needs ICE + STUN); we validate the
HTTP signaling contract:
- 404 for an unknown camera_id
- 400 for bad JSON / wrong body shape
- 200 with {sdp, type:'answer'} for a valid offer
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from aiortc import RTCPeerConnection, RTCSessionDescription

from carlabridge.obs.event_log import EventLog
from carlabridge.sensors.camera import CameraManager
from carlabridge.streaming.webrtc import shutdown_peer_connections, signaling_route


@pytest.fixture
async def signaling_client():
    mgr = CameraManager()
    mgr.get_or_create_queue("city").bind_loop()
    app = web.Application()
    app.router.add_post(
        "/webrtc/{camera_id}", signaling_route(mgr, EventLog(capacity=50))
    )
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, mgr
    finally:
        await client.close()
        await shutdown_peer_connections()


async def test_signaling_unknown_camera(signaling_client):
    client, _ = signaling_client
    resp = await client.post(
        "/webrtc/nope", json={"sdp": "x", "type": "offer"}
    )
    assert resp.status == 404


async def test_signaling_bad_body(signaling_client):
    client, _ = signaling_client
    resp = await client.post("/webrtc/city", data="not json")
    assert resp.status == 400
    resp = await client.post("/webrtc/city", json={"sdp": "x", "type": "answer"})
    assert resp.status == 400


async def test_signaling_full_offer_answer_roundtrip(signaling_client):
    client, _ = signaling_client
    pc = RTCPeerConnection()
    try:
        pc.addTransceiver("video", direction="recvonly")
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        # Wait briefly for ICE gathering — same shape as frontend's flow.
        for _ in range(40):
            if pc.iceGatheringState == "complete":
                break
            await asyncio.sleep(0.05)

        local = pc.localDescription
        resp = await client.post(
            "/webrtc/city", json={"sdp": local.sdp, "type": local.type}
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["type"] == "answer"
        assert "sdp" in body and "v=0" in body["sdp"]
        # Drive the peer to remote-description so it transitions toward connected.
        await pc.setRemoteDescription(RTCSessionDescription(**body))
    finally:
        await pc.close()
