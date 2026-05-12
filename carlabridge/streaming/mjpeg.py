"""MJPEG fallback (spec §F2.2).

GET /video_feed?camera=<id> returns `multipart/x-mixed-replace; boundary=frame`,
serving JPEG-encoded frames from the shared FrameQueue. The route only spins
up while a client is connected — the producer (CARLA sensor) is unaffected
either way (it pushes into FrameQueue regardless of subscribers).

Frame cadence is the queue's natural rate (the same one CARLA sensor.tick
delivers). Each client opens its own consumer loop, but they share the queue,
so two simultaneous MJPEG clients on the same channel will each see roughly
half the frames (FrameQueue is single-consumer). For the demo's intended
"fallback when WebRTC is down" semantics this is acceptable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import web

from carlabridge.streaming.jpeg_tap import encode_jpeg

if TYPE_CHECKING:  # pragma: no cover
    from carlabridge.sensors.camera import CameraManager

log = logging.getLogger(__name__)

BOUNDARY = "frame"


def mjpeg_route(camera_manager: "CameraManager"):
    """Returns an aiohttp handler for `GET /video_feed?camera=<id>`."""

    async def handle(request: web.Request) -> web.StreamResponse:
        camera_id = request.query.get("camera", "").strip()
        if not camera_id:
            return web.Response(status=400, text="missing ?camera=<id>")
        queue = camera_manager.queue_for(camera_id)
        if queue is None:
            return web.Response(status=404, text=f"unknown camera: {camera_id}")

        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Connection": "close",
                "Pragma": "no-cache",
            },
        )
        await resp.prepare(request)
        log.info("mjpeg[%s] client connected", camera_id)

        frames_sent = 0
        try:
            while True:
                # `queue.get()` awaits the next frame; the queue clears the slot
                # so we never serve a stale frame.
                rgb = await queue.get()
                try:
                    jpeg = encode_jpeg(rgb)
                except Exception:
                    log.exception("mjpeg[%s] JPEG encode failed", camera_id)
                    continue
                part = (
                    f"--{BOUNDARY}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n\r\n"
                ).encode("ascii") + jpeg + b"\r\n"
                try:
                    await resp.write(part)
                except (ConnectionResetError, asyncio.CancelledError):
                    log.info("mjpeg[%s] client dropped", camera_id)
                    break
                frames_sent += 1
        finally:
            log.info("mjpeg[%s] client closed (%d frames sent)", camera_id, frames_sent)
            try:
                await resp.write_eof()
            except Exception:  # pragma: no cover -- best-effort
                pass
        return resp

    return handle


__all__ = ["mjpeg_route", "BOUNDARY"]
