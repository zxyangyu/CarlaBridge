"""Process entrypoint.

M0: HTTP + Socket.IO skeleton.
M1: + CARLA connection, sync mode, tick thread (NoopScenario placeholder).
M2: + WorldSnapshot atomic ref + 10Hz broadcaster (state_update / state_snapshot).
M3: + city camera (world_pose) + WebRTC signaling (POST /webrtc/{camera_id}).

Usage:
    python -m carlabridge.main [--config PATH] [--scenario NAME] [--log-level LVL]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from aiohttp import web

from carlabridge.bus.broadcaster import Broadcaster
from carlabridge.bus.projector import FocusBinding
from carlabridge.bus.server import build_app
from carlabridge.config import Settings, load_settings
from carlabridge.core.atomic import AtomicRef
from carlabridge.core.clock import SimClock
from carlabridge.core.fleet import Fleet
from carlabridge.core.snapshot import SnapshotBuilder, WorldSnapshot
from carlabridge.core.tick_loop import NoopScenario, TickLoop
from carlabridge.core.world import BridgeFatal, World
from carlabridge.obs.event_log import EventLog
from carlabridge.obs.metrics import Metrics
from carlabridge.sensors.camera import CameraBinding, CameraManager, CameraSpec
from carlabridge.streaming.webrtc import shutdown_peer_connections

log = logging.getLogger("carlabridge")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # aiohttp access logs are noisy under polling transport; suppress to WARN.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="carlabridge")
    p.add_argument("--config", type=Path, default=None, help="extra TOML overlay")
    p.add_argument("--scenario", type=str, default=None, help="override scenario.default")
    p.add_argument("--log-level", type=str, default=None, help="DEBUG/INFO/WARN/ERROR")
    p.add_argument("--no-carla", action="store_true",
                   help="skip CARLA connection (dev only — for HTTP-only smoke tests)")
    return p.parse_args(argv)


def _seed_bindings(camera_manager: CameraManager) -> None:
    """M4 default channel layout. Scenarios (M5) override aerial/ground."""
    # city: world_pose high overhead, Town10HD-friendly z=200, pitch -90°
    camera_manager.bind(CameraBinding(spec=CameraSpec(
        id="city", mode="world_pose",
        x=0.0, y=0.0, z=200.0,
        pitch=-90.0, yaw=0.0, roll=0.0,
        fov=90.0, width=1280, height=720, fps=25,
    )))
    # aerial: follows_virtual UAV — scenario must set attach_entity_id at setup.
    camera_manager.bind(CameraBinding(spec=CameraSpec(
        id="aerial", mode="follows_virtual",
        # Offset relative to UAV's world pose (M4 uses world-frame offset).
        x=0.0, y=0.0, z=20.0,
        pitch=-30.0, yaw=0.0, roll=0.0,
        fov=90.0, width=1280, height=720, fps=25,
        attach_entity_id=None,  # scenario fills this in
    )))
    # ground: attached_to_actor UGV — scenario must set attach_entity_id.
    camera_manager.bind(CameraBinding(spec=CameraSpec(
        id="ground", mode="attached_to_actor",
        # Offset in actor-local frame: above + slightly behind.
        x=-3.0, y=0.0, z=2.0,
        pitch=-10.0, yaw=0.0, roll=0.0,
        fov=70.0, width=1280, height=720, fps=25,
        attach_entity_id=None,
    )))


def _apply_cli_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    if args.scenario:
        settings.scenario.default = args.scenario
    if args.log_level:
        settings.logging.level = args.log_level
    return settings


async def _run(settings: Settings, *, no_carla: bool) -> int:
    event_log = EventLog(capacity=settings.logging.event_log_buffer)
    metrics = Metrics()
    fleet = Fleet()
    event_log.add("ok", "BRIDGE", "CarlaBridge starting")

    # Snapshot infra is created up-front so the http server can hand it to
    # the namespaces (on_connect emits depend on it).
    snapshot_ref: AtomicRef[WorldSnapshot] = AtomicRef()
    focus = FocusBinding()
    camera_manager = CameraManager()
    # M4: pre-bind the three frontend channels (aerial / ground / city) so
    # the signaling route and on-connect snapshot find their FrameQueues
    # immediately. aerial / ground stay unspawned until a scenario (M5) sets
    # their attach_entity_id; city is unattached (world_pose) and spawns now.
    _seed_bindings(camera_manager)

    world: World | None = None
    tick_loop: TickLoop | None = None
    runner: web.AppRunner | None = None
    broadcaster: Broadcaster | None = None
    exit_code = 0

    try:
        # ---------- 1. CARLA connect + sync mode ------------------------
        if not no_carla:
            try:
                world = World.connect(
                    settings.carla.host, settings.carla.port, settings.carla.timeout_s
                )
                world.save_original_settings()
                world.ensure_map(settings.carla.map)
                world.switch_to_sync(settings.carla.fixed_delta_seconds)
                event_log.add(
                    "ok",
                    "CARLA",
                    f"connected, map={world.current_map_name()}, "
                    f"sync delta={settings.carla.fixed_delta_seconds:.4f}s",
                )
            except BridgeFatal as e:
                log.error("fatal: %s", e)
                event_log.add("danger", "CARLA", f"connect failed: {e}")
                return 2
        else:
            log.warning("--no-carla: HTTP/Socket.IO only, no tick loop")
            event_log.add("warn", "BRIDGE", "started in --no-carla mode")

        # ---------- 2. HTTP + Socket.IO ---------------------------------
        stop_event = asyncio.Event()
        app, sio = build_app(
            settings,
            event_log,
            metrics,
            snapshot_ref=snapshot_ref,
            focus=focus,
            camera_manager=camera_manager,
            shutdown_event=stop_event,
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, settings.server.host, settings.server.port)
        try:
            await site.start()
        except OSError as e:
            if getattr(e, "errno", None) == 10048 or "address" in str(e).lower():
                log.error(
                    "port %d already in use — stop the conflicting process and retry",
                    settings.server.port,
                )
                event_log.add(
                    "danger",
                    "BRIDGE",
                    f"port {settings.server.port} already in use",
                )
                return 3
            raise
        log.info(
            "listening on http://%s:%d  (carla=%s:%d  map=%s  agent=%s  scenario=%s)",
            settings.server.host,
            settings.server.port,
            settings.carla.host,
            settings.carla.port,
            settings.carla.map,
            settings.agent.mode,
            settings.scenario.default,
        )
        event_log.add(
            "ok",
            "BRIDGE",
            f"http listening on {settings.server.host}:{settings.server.port}",
        )

        # ---------- 3. bind frame queues to loop + spawn cameras --------
        # Bind all known queues to the running loop before any producer fires.
        for q in camera_manager.queues.values():
            q.bind_loop()
        if world is not None:
            try:
                camera_manager.spawn_all(world.carla_world, fleet)
                for cid in camera_manager.cameras:
                    event_log.add("ok", "CARLA", f"camera {cid} spawned")
            except Exception as e:
                log.exception("camera spawn_all failed")
                event_log.add("danger", "CARLA", f"camera spawn_all failed: {e}")

        # ---------- 4. tick thread + snapshot builder (NoopScenario) ----
        if world is not None:
            clock = SimClock(delta=settings.carla.fixed_delta_seconds)
            snapshot_builder = SnapshotBuilder(world=world.carla_world)
            tick_loop = TickLoop(
                world=world,
                clock=clock,
                fleet=fleet,
                scenario=NoopScenario(),
                metrics=metrics,
                event_log=event_log,
                snapshot_builder=snapshot_builder,
                snapshot_ref=snapshot_ref,
                camera_manager=camera_manager,
            )
            tick_loop.start()
            event_log.add("ok", "BRIDGE", "tick loop running (NoopScenario)")

        # ---------- 5. broadcaster --------------------------------------
        broadcaster = Broadcaster(
            sio=sio,
            snapshot_ref=snapshot_ref,
            focus=focus,
            metrics=metrics,
            state_hz=settings.broadcast.state_hz,
            metrics_hz=settings.broadcast.metrics_hz,
        )
        broadcaster.start()

        # ---------- 6. wait for shutdown --------------------------------
        loop = asyncio.get_running_loop()

        def _request_stop() -> None:
            if not stop_event.is_set():
                log.info("shutdown requested")
                stop_event.set()

        try:
            loop.add_signal_handler(signal.SIGINT, _request_stop)
            loop.add_signal_handler(signal.SIGTERM, _request_stop)
        except NotImplementedError:  # Windows fallback
            signal.signal(signal.SIGINT, lambda *_: _request_stop())

        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            _request_stop()
    finally:
        event_log.add("warn", "BRIDGE", "shutting down")
        # 7a. stop broadcaster before HTTP teardown
        if broadcaster is not None:
            await broadcaster.stop()
        # 7b. close all WebRTC peer connections (also drains encoder tasks)
        await shutdown_peer_connections()
        # 7c. stop tick thread BEFORE restoring CARLA (no ticks during restore)
        if tick_loop is not None:
            log.info("stopping tick loop…")
            tick_loop.stop()
            tick_loop.join(timeout=3.0)
        # 7d. detach CARLA cameras BEFORE restoring async mode (cameras are
        #     CARLA actors; in async mode their listener semantics change)
        try:
            camera_manager.detach_all()
        except Exception:
            log.exception("camera detach_all failed")
        # 7e. drain HTTP / Socket.IO
        if runner is not None:
            await runner.cleanup()
        # 7f. restore CARLA async mode + disconnect (CRITICAL — runs even
        #     if startup failed after switch_to_sync)
        if world is not None:
            try:
                world.restore_original_settings()
            finally:
                world.disconnect()
        log.info("bye")

    return exit_code


def cli(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = load_settings(extra_config=args.config)
    settings = _apply_cli_overrides(settings, args)
    _configure_logging(settings.logging.level)
    try:
        return asyncio.run(_run(settings, no_carla=args.no_carla))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(cli())
