"""WebRTC video streaming via aiortc.

Two pieces:

1. `CameraTrack` — a `VideoStreamTrack` that pulls frames from a `FrameQueue`
   and yields them as `av.VideoFrame` at the queue's natural rate.

2. `signaling_route(camera_manager)` — aiohttp handler factory for
   `POST /webrtc/{camera_id}`. Matches the frontend contract:
       request:  {"sdp": "...", "type": "offer"}
       response: {"sdp": "...", "type": "answer"}

   Per-session state (RTCPeerConnection) lives in a module-level set so we can
   close them on shutdown.

Frontend transceivers — the React side adds both video + audio recvonly
transceivers. aiortc auto-negotiates them; we only ever attach a video track.
"""

from __future__ import annotations

import asyncio
import fractions
import logging
import time
from typing import TYPE_CHECKING

import numpy as np
from aiohttp import web
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)
from av import VideoFrame

from carlabridge.obs.event_log import EventLog
from carlabridge.sensors.frame_queue import FrameQueue

if TYPE_CHECKING:  # pragma: no cover
    from carlabridge.sensors.camera import CameraManager

log = logging.getLogger(__name__)

# Active peer connections — used for cleanup on app shutdown. Tracks include
# their owning channel so logs are meaningful.
_PEER_CONNECTIONS: set[RTCPeerConnection] = set()


# ---------- track ---------------------------------------------------------


class CameraTrack(VideoStreamTrack):
    """Pulls `numpy.ndarray (H,W,3) uint8 RGB` out of a FrameQueue.

    `recv()` is called by aiortc's internal encoder loop. We block (await) on
    the queue so the encoder pulls at the producer's natural rate; no frame
    repetition for now (browsers tolerate variable cadence inside an RTP video
    track via the rtpmap).
    """

    # 90 kHz is the canonical clock-rate for video RTP — aiortc uses it
    # internally too. next_timestamp() gives us a monotonic 90 kHz pts.
    kind = "video"

    def __init__(self, queue: FrameQueue, *, channel_id: str = "?") -> None:
        super().__init__()
        self._queue = queue
        self._channel_id = channel_id
        self._first_recv_ts: float | None = None
        self._frames_sent = 0

    async def recv(self) -> VideoFrame:
        # next_timestamp returns (pts, time_base) — pts on a 90kHz clock that
        # matches the negotiated rtpmap, so receivers play back at real time.
        pts, time_base = await self.next_timestamp()
        arr = await self._queue.get()
        if not isinstance(arr, np.ndarray):
            raise TypeError(
                f"CameraTrack[{self._channel_id}]: queue produced {type(arr)}, want ndarray"
            )
        frame = VideoFrame.from_ndarray(arr, format="rgb24")
        frame.pts = pts
        frame.time_base = time_base
        self._frames_sent += 1
        if self._first_recv_ts is None:
            self._first_recv_ts = time.monotonic()
            log.info("CameraTrack[%s] first frame delivered", self._channel_id)
        return frame


# ---------- signaling -----------------------------------------------------


async def _close_pc(pc: RTCPeerConnection) -> None:
    try:
        await pc.close()
    finally:
        _PEER_CONNECTIONS.discard(pc)


def signaling_route(camera_manager: "CameraManager", event_log: EventLog):
    """Return an aiohttp handler bound to a CameraManager.

    Use:
        app.router.add_post("/webrtc/{camera_id}", signaling_route(mgr, ev))
    """

    async def handle(request: web.Request) -> web.Response:
        camera_id = request.match_info["camera_id"]
        queue = camera_manager.queue_for(camera_id)
        if queue is None:
            return web.json_response(
                {"error": f"unknown camera: {camera_id}"}, status=404
            )

        try:
            params = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)
        sdp = params.get("sdp")
        sdp_type = params.get("type")
        if not sdp or sdp_type != "offer":
            return web.json_response(
                {"error": "body must be {sdp, type:'offer'}"}, status=400
            )

        pc = RTCPeerConnection()
        _PEER_CONNECTIONS.add(pc)
        track = CameraTrack(queue, channel_id=camera_id)
        pc.addTrack(track)

        @pc.on("connectionstatechange")
        async def _on_state_change() -> None:  # noqa: D401
            state = pc.connectionState
            log.info("webrtc[%s] state=%s", camera_id, state)
            if state in ("failed", "closed", "disconnected"):
                event_log.add(
                    "info" if state == "closed" else "warn",
                    "BRIDGE",
                    f"webrtc[{camera_id}] {state}",
                )
                await _close_pc(pc)

        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
        except Exception as e:
            log.exception("webrtc[%s] signaling failed", camera_id)
            await _close_pc(pc)
            return web.json_response({"error": f"signaling failed: {e}"}, status=500)

        event_log.add("ok", "BRIDGE", f"webrtc[{camera_id}] peer connected")
        local = pc.localDescription
        return web.json_response({"sdp": local.sdp, "type": local.type})

    return handle


async def shutdown_peer_connections() -> None:
    """Best-effort close of every still-open peer connection. Call at teardown."""
    to_close = list(_PEER_CONNECTIONS)
    if not to_close:
        return
    log.info("closing %d webrtc peer connections", len(to_close))
    await asyncio.gather(*(_close_pc(pc) for pc in to_close), return_exceptions=True)


__all__ = [
    "CameraTrack",
    "signaling_route",
    "shutdown_peer_connections",
]
