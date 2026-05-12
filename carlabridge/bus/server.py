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

from carlabridge.agent.link import AgentLink
from carlabridge.bus.agent_ns import AgentNamespace
from carlabridge.bus.frontend_ns import FrontendNamespace
from carlabridge.bus.projector import FocusBinding
from carlabridge.commands.bus import CommandBus
from carlabridge.config import Settings
from carlabridge.core.atomic import AtomicRef
from carlabridge.core.snapshot import WorldSnapshot
from carlabridge.obs.event_log import EventLog
from carlabridge.obs.metrics import Metrics
from carlabridge.sensors.camera import CameraManager
from carlabridge.streaming.mjpeg import mjpeg_route
from carlabridge.streaming.webrtc import signaling_route

log = logging.getLogger(__name__)


def make_sio(settings: Settings) -> socketio.AsyncServer:
    """Standalone SocketIO server construction so callers can wire bus/link
    BEFORE namespaces register their event handlers."""
    return socketio.AsyncServer(
        async_mode="aiohttp",
        cors_allowed_origins=_cors_origins(settings.server.cors_origins),
    )


def build_app(
    settings: Settings,
    event_log: EventLog,
    metrics: Metrics,
    *,
    sio: socketio.AsyncServer,
    snapshot_ref: AtomicRef[WorldSnapshot],
    focus: FocusBinding,
    camera_manager: CameraManager,
    command_bus: CommandBus | None = None,
    agent_link: AgentLink | None = None,
    shutdown_event: "asyncio.Event | None" = None,
) -> tuple[web.Application, socketio.AsyncServer]:
    """Build the aiohttp app with the (already-constructed) Socket.IO server
    attached and namespaces registered.

    If `shutdown_event` is provided, exposes `POST /admin/shutdown` that sets it.
    """
    frontend_ns = FrontendNamespace(
        "/",
        event_log=event_log,
        snapshot_ref=snapshot_ref,
        focus=focus,
        agent_link=agent_link,
    )
    agent_ns = AgentNamespace(
        "/agent",
        event_log=event_log,
        snapshot_ref=snapshot_ref,
        command_bus=command_bus,
    )
    sio.register_namespace(frontend_ns)
    sio.register_namespace(agent_ns)

    app = web.Application()
    sio.attach(app)

    app["settings"] = settings
    app["event_log"] = event_log
    app["metrics"] = metrics
    app["sio"] = sio
    app["snapshot_ref"] = snapshot_ref
    app["focus"] = focus
    app["camera_manager"] = camera_manager
    app["command_bus"] = command_bus
    app["agent_link"] = agent_link
    app["frontend_ns"] = frontend_ns
    app["agent_ns"] = agent_ns
    app["shutdown_event"] = shutdown_event
    # Mutable holder for late-bound dependencies (scenario_runner constructed
    # AFTER build_app in main). aiohttp deprecates writing new keys after
    # AppRunner.setup() — we mutate this dict instead.
    app["late"] = {"scenario_runner": None}

    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/debug/events", _debug_events)
    app.router.add_post("/admin/shutdown", _shutdown)
    app.router.add_post(
        "/webrtc/{camera_id}", signaling_route(camera_manager, event_log)
    )
    app.router.add_get("/video_feed", mjpeg_route(camera_manager))

    return app, sio


def _cors_origins(origins: list[str]) -> str | list[str]:
    # python-socketio accepts "*" to allow everything; otherwise an exact list.
    if not origins or "*" in origins:
        return "*"
    return list(origins)


async def _healthz(request: web.Request) -> web.Response:
    """Full health endpoint (design §15.4)."""
    settings: Settings = request.app["settings"]
    metrics: Metrics = request.app["metrics"]
    snap_ref: AtomicRef[WorldSnapshot] = request.app["snapshot_ref"]
    cam_mgr: CameraManager = request.app["camera_manager"]
    frontend_ns: FrontendNamespace = request.app["frontend_ns"]
    agent_ns: AgentNamespace = request.app["agent_ns"]
    command_bus: CommandBus | None = request.app["command_bus"]
    scenario_runner = request.app["late"].get("scenario_runner")
    snap = snap_ref.get()
    m = metrics.snapshot()

    # CARLA reachability is inferred from snapshot freshness — a snapshot
    # exists iff the tick loop is running, which implies a live RPC link.
    carla_state = "connected" if snap is not None else "disconnected"

    # Scenario state: "idle" / "running" / "stopped" / "failed" + name.
    if scenario_runner is None:
        scenario_state = "none/idle"
    else:
        scenario_state = f"{scenario_runner.name}/{scenario_runner.state}"

    # Per-channel camera health: ok if (a) spawned, (b) producing frames
    # (produced > consumed at least once in the last sample window — proxy:
    # produced > 0).
    cameras_health: dict[str, dict] = {}
    for cid, q in cam_mgr.queues.items():
        spawned = cid in cam_mgr.cameras
        if not spawned:
            status = "unbound"
        elif q.produced == 0:
            status = "spawned-no-frames"
        else:
            status = "ok"
        cameras_health[cid] = {"status": status, **q.stats()}

    payload = {
        "status": "alive",
        "version": "0.1.0",
        "carla": carla_state,
        "tick_fps": m.get("tick_fps", 0),
        "scenario": scenario_state,
        "clients": {
            "frontend": frontend_ns.client_count,
            "agent": agent_ns.client_count,
        },
        "cameras": cameras_health,
        "config": {
            "carla_map": settings.carla.map,
            "tick_hz": round(1.0 / settings.carla.fixed_delta_seconds, 1),
            "agent_mode": settings.agent.mode,
            "scenario_default": settings.scenario.default,
        },
        "metrics": m,
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
        "command_bus": (
            {"depth": command_bus.depth()} if command_bus is not None else None
        ),
    }
    return web.json_response(payload)


async def _debug_events(request: web.Request) -> web.Response:
    """Dump the event_log ring buffer as JSON. Diagnostic only."""
    event_log: EventLog = request.app["event_log"]
    n = int(request.query.get("n", "200"))
    return web.json_response(
        {"events": [e.to_dict() for e in event_log.recent(n)]}
    )


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
