"""Socket.IO + aiohttp server assembly.

M0 wired empty namespaces. M2 plugs in the snapshot AtomicRef + FocusBinding
so both namespaces can emit on-connect, and exposes the SocketIO handle so
main can spin up the Broadcaster.
"""

from __future__ import annotations

import asyncio
import logging

import socketio
from aiohttp import web

from carlabridge.bus.agent_ns import AgentNamespace
from carlabridge.bus.frontend_ns import FrontendNamespace
from carlabridge.bus.projector import FocusBinding
from carlabridge.config import Settings
from carlabridge.core.atomic import AtomicRef
from carlabridge.core.snapshot import WorldSnapshot
from carlabridge.obs.event_log import EventLog
from carlabridge.obs.metrics import Metrics
from carlabridge.sensors.camera import CameraManager
from carlabridge.streaming.webrtc import signaling_route

log = logging.getLogger(__name__)


def build_app(
    settings: Settings,
    event_log: EventLog,
    metrics: Metrics,
    *,
    snapshot_ref: AtomicRef[WorldSnapshot],
    focus: FocusBinding,
    camera_manager: CameraManager,
    shutdown_event: "asyncio.Event | None" = None,
) -> tuple[web.Application, socketio.AsyncServer]:
    """Build the aiohttp app with Socket.IO attached. Caller runs it.

    If `shutdown_event` is provided, exposes `POST /admin/shutdown` that sets it.
    Used for graceful programmatic shutdown (testing + ops).
    """
    sio = socketio.AsyncServer(
        async_mode="aiohttp",
        cors_allowed_origins=_cors_origins(settings.server.cors_origins),
        # Pings are critical for Win10 stability; defaults are fine for now.
    )

    sio.register_namespace(
        FrontendNamespace(
            "/", event_log=event_log, snapshot_ref=snapshot_ref, focus=focus
        )
    )
    sio.register_namespace(
        AgentNamespace("/agent", event_log=event_log, snapshot_ref=snapshot_ref)
    )

    app = web.Application()
    sio.attach(app)

    app["settings"] = settings
    app["event_log"] = event_log
    app["metrics"] = metrics
    app["sio"] = sio
    app["snapshot_ref"] = snapshot_ref
    app["focus"] = focus
    app["camera_manager"] = camera_manager
    app["shutdown_event"] = shutdown_event

    app.router.add_get("/healthz", _healthz)
    app.router.add_post("/admin/shutdown", _shutdown)
    app.router.add_post(
        "/webrtc/{camera_id}", signaling_route(camera_manager, event_log)
    )

    return app, sio


def _cors_origins(origins: list[str]) -> str | list[str]:
    # python-socketio accepts "*" to allow everything; otherwise an exact list.
    if not origins or "*" in origins:
        return "*"
    return list(origins)


async def _healthz(request: web.Request) -> web.Response:
    """M0 placeholder. M7 expands to carla / tick_fps / scenario / clients / cameras."""
    settings: Settings = request.app["settings"]
    metrics: Metrics = request.app["metrics"]
    snap_ref: AtomicRef[WorldSnapshot] = request.app["snapshot_ref"]
    cam_mgr: CameraManager = request.app["camera_manager"]
    snap = snap_ref.get()
    payload = {
        "status": "alive",
        "version": "0.1.0",
        "config": {
            "carla_map": settings.carla.map,
            "tick_hz": round(1.0 / settings.carla.fixed_delta_seconds, 1),
            "agent_mode": settings.agent.mode,
            "scenario_default": settings.scenario.default,
        },
        "metrics": metrics.snapshot(),
        "snapshot": {
            "available": snap is not None,
            "sim_time": snap.sim_time if snap is not None else None,
            "counts": (
                {
                    "traffic_lights": len(snap.traffic_lights),
                    "vehicles": len(snap.vehicles),
                    "uavs": len(snap.uavs),
                }
                if snap is not None
                else None
            ),
        },
        "cameras": {
            cid: {"spawned": cid in cam_mgr.cameras, **q.stats()}
            for cid, q in cam_mgr.queues.items()
        },
    }
    return web.json_response(payload)


async def _shutdown(request: web.Request) -> web.Response:
    """POST /admin/shutdown — triggers graceful shutdown via the stop_event.

    Returns 200 immediately; the actual teardown runs after this response
    flushes (the main loop wakes from await stop_event.wait()).
    """
    ev: asyncio.Event | None = request.app["shutdown_event"]
    if ev is None:
        return web.json_response(
            {"error": "shutdown_event not wired"}, status=503
        )
    event_log: EventLog = request.app["event_log"]
    event_log.add("warn", "BRIDGE", "shutdown requested via /admin/shutdown")
    ev.set()
    return web.json_response({"status": "shutting down"})
