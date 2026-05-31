"""Process entrypoint.

History:
  M0..M6 — initial milestones (HTTP, CARLA tick, snapshot/broadcaster,
           cameras + WebRTC, scenario engine, command bus).
  Refactor v0.3 — Agent / Bridge decoupling: Bridge waits passively for a
           remote Agent over Socket.IO (no in-process mock); HTTP
           ``/scenario/{fire,reset,status}`` control plane.

Usage:
    python -m carlabridge.main [--config PATH] [--scenario NAME] [--log-level LVL]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import uuid
from pathlib import Path

from aiohttp import web

from carlabridge.bus.broadcaster import Broadcaster
from carlabridge.bus.projector import FocusBinding
from carlabridge.bus.server import build_app, make_sio
from carlabridge.commands.bus import CommandBus
from carlabridge.config import Settings, load_settings
from carlabridge.core.atomic import AtomicRef
from carlabridge.core.clock import SimClock
from carlabridge.core.fleet import Fleet
from carlabridge.core.snapshot import SnapshotBuilder, WorldSnapshot
from carlabridge.core.tick_loop import NoopScenario, TickLoop
from carlabridge.core.world import BridgeFatal, World
from carlabridge.obs.event_log import EventLog
from carlabridge.obs.metrics import Metrics
# Importing the scenarios package triggers @register_scenario side effects.
from carlabridge.scenarios import available_scenarios, get_scenario_class  # noqa: F401
from carlabridge.scenarios.runner import ScenarioRunner
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
    p.add_argument(
        "--record-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="save each camera frame as PNG under DIR/<camera_id>/ (ffmpeg to MP4 after run)",
    )
    return p.parse_args(argv)


def _seed_bindings(camera_manager: CameraManager, settings: Settings) -> None:
    """M4 default channel layout. Scenarios (M5) override aerial/ground."""
    video = settings.video
    city_w, city_h = video.channel_resolution("city")
    aerial_w, aerial_h = video.channel_resolution("aerial")
    ground_w, ground_h = video.channel_resolution("ground")
    city_pose = settings.camera.city
    camera_manager.bind(CameraBinding(spec=CameraSpec(
        id="city", mode="world_pose",
        x=city_pose.x, y=city_pose.y, z=city_pose.z,
        pitch=city_pose.pitch, yaw=city_pose.yaw, roll=city_pose.roll,
        fov=city_pose.fov,
        width=city_w, height=city_h, fps=video.channel_fps("city"),
    )))
    # aerial: follows_virtual UAV — scenario must set attach_entity_id at setup.
    camera_manager.bind(CameraBinding(spec=CameraSpec(
        id="aerial", mode="follows_virtual",
        # Offset relative to UAV's world pose (M4 uses world-frame offset).
        x=0.0, y=0.0, z=10.0,
        pitch=-50.0, yaw=0.0, roll=0.0,
        fov=90.0,
        width=aerial_w, height=aerial_h, fps=video.channel_fps("aerial"),
        attach_entity_id=None,  # scenario fills this in
    )))
    # ground: attached_to_actor UGV (s1_fire: vehicle.carlamotors.firetruck).
    camera_manager.bind(CameraBinding(spec=CameraSpec(
        id="ground", mode="attached_to_actor",
        # Actor-local: behind cab + above roof (sedan was z=2; firetruck ~+1.8 m).
        x=-4.5, y=0, z=6, pitch=-25, yaw=0.0, roll=0.0,
        fov=70.0,
        width=ground_w, height=ground_h, fps=video.channel_fps("ground"),
        attach_entity_id=None,
    )))


def _apply_cli_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    if args.scenario:
        settings.scenario.default = args.scenario
    if args.log_level:
        settings.logging.level = args.log_level
    return settings


async def _run(
    settings: Settings, *, no_carla: bool, record_dir: Path | None = None
) -> int:
    event_log = EventLog(capacity=settings.logging.event_log_buffer)
    metrics = Metrics()
    fleet = Fleet()
    bridge_session_id = f"br-{uuid.uuid4().hex[:8]}"
    event_log.add(
        "ok", "BRIDGE", f"CarlaBridge starting (session={bridge_session_id})"
    )

    # Snapshot infra is created up-front so the http server can hand it to
    # the namespaces (on_connect emits depend on it).
    snapshot_ref: AtomicRef[WorldSnapshot] = AtomicRef()
    focus = FocusBinding()
    camera_manager = CameraManager(record_base=record_dir)
    if record_dir is not None:
        event_log.add("ok", "BRIDGE", f"recording PNGs under {record_dir}")
    # M4: pre-bind the three frontend channels (aerial / ground / city) so
    # the signaling route and on-connect snapshot find their FrameQueues
    # immediately. aerial / ground stay unspawned until a scenario (M5) sets
    # their attach_entity_id; city is unattached (world_pose) and spawns now.
    _seed_bindings(camera_manager, settings)

    world: World | None = None
    tick_loop: TickLoop | None = None
    runner: web.AppRunner | None = None
    broadcaster: Broadcaster | None = None
    scenario_runner: ScenarioRunner | None = None
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

        # ---------- 2. HTTP + Socket.IO + CommandBus --------------------
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        sio = make_sio(settings)
        command_bus = CommandBus(loop=loop, sio=sio, event_log=event_log)
        app, sio = build_app(
            settings,
            event_log,
            metrics,
            sio=sio,
            snapshot_ref=snapshot_ref,
            focus=focus,
            camera_manager=camera_manager,
            command_bus=command_bus,
            shutdown_event=stop_event,
            bridge_session_id=bridge_session_id,
            scenario_name=settings.scenario.default,
        )
        agent_ns = app["agent_ns"]

        # Sim → async hop for command_status / scenario_event broadcasts.
        # The scenario calls bus.broadcast_* on the tick thread; we schedule
        # the actual socket emit onto the asyncio loop.
        def _emit_command_status(payload: dict) -> None:
            loop.call_soon_threadsafe(
                sio.start_background_task,
                agent_ns.broadcast_command_status,
                payload,
            )

        def _emit_scenario_event(payload: dict) -> None:
            loop.call_soon_threadsafe(
                sio.start_background_task,
                agent_ns.broadcast_scenario_event,
                payload,
            )

        command_bus.set_on_command_status(_emit_command_status)
        command_bus.set_on_scenario_event(_emit_scenario_event)
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
            "listening on http://%s:%d  (carla=%s:%d  map=%s  scenario=%s  session=%s)",
            settings.server.host,
            settings.server.port,
            settings.carla.host,
            settings.carla.port,
            settings.carla.map,
            settings.scenario.default,
            bridge_session_id,
        )
        event_log.add(
            "ok",
            "BRIDGE",
            f"http listening on {settings.server.host}:{settings.server.port}",
        )

        # ---------- 3. bind frame queues to loop + spawn city camera ----
        # Bind all known queues to the running loop before any producer fires.
        for q in camera_manager.queues.values():
            q.bind_loop()
        if world is not None:
            try:
                # Spawns city (world_pose) immediately; aerial/ground are
                # skipped here because their attach_entity_id is still None.
                # The scenario fills them in below via `rebind`.
                camera_manager.spawn_all(world.carla_world, fleet)
                for cid in camera_manager.cameras:
                    event_log.add("ok", "CARLA", f"camera {cid} spawned")
            except Exception as e:
                log.exception("camera spawn_all failed")
                event_log.add("danger", "CARLA", f"camera spawn_all failed: {e}")

        # ---------- 4. scenario setup (spawns UGV + virtual UAVs +
        #                              fire marker + rebinds aerial/ground)
        scenario = NoopScenario()
        if world is not None:
            try:
                scenario_cls = get_scenario_class(settings.scenario.default)
            except KeyError as e:
                log.error("scenario lookup failed: %s", e)
                event_log.add("danger", "SCENARIO", f"unknown scenario: {e}")
                return 4
            clock = SimClock(delta=settings.carla.fixed_delta_seconds)
            scenario_runner = ScenarioRunner(
                scenario_cls,
                world=world,
                fleet=fleet,
                camera_manager=camera_manager,
                event_log=event_log,
                sim_time_provider=lambda c=clock: c.sim_time,
                command_bus=command_bus,
                settings=settings,
            )
            try:
                scenario = scenario_runner.start()
            except Exception as e:
                log.exception("scenario setup failed")
                event_log.add(
                    "danger", "SCENARIO", f"setup failed: {type(e).__name__}: {e}"
                )
                return 5
            # Late-wire scenario-aware providers into the agent namespace.
            agent_ns.set_resetting_provider(scenario_runner.is_resetting)
            agent_ns.set_sim_time_provider(scenario_runner.sim_time)

        # ---------- 5. tick thread --------------------------------------
        if world is not None:
            snapshot_builder = SnapshotBuilder(world=world.carla_world)
            tick_loop = TickLoop(
                world=world,
                clock=clock,
                fleet=fleet,
                scenario=scenario,
                metrics=metrics,
                event_log=event_log,
                snapshot_builder=snapshot_builder,
                snapshot_ref=snapshot_ref,
                camera_manager=camera_manager,
                command_bus=command_bus,
                bridge_session_id=bridge_session_id,
                sim_task_drain=scenario_runner.drain_sim_tasks,
            )
            tick_loop.start()
            event_log.add(
                "ok", "BRIDGE", f"tick loop running (scenario={scenario.name})"
            )

        # ---------- 6. broadcaster --------------------------------------
        broadcaster = Broadcaster(
            sio=sio,
            snapshot_ref=snapshot_ref,
            focus=focus,
            metrics=metrics,
            event_log=event_log,
            state_hz=settings.broadcast.state_hz,
            metrics_hz=settings.broadcast.metrics_hz,
        )
        broadcaster.start()
        # Stash the scenario_runner for /healthz (None ok). Mutating the
        # pre-created `late` dict avoids aiohttp's post-freeze write warning.
        app["late"]["scenario_runner"] = scenario_runner

        # ---------- 7. wait for shutdown --------------------------------
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
        # 8b. stop broadcaster before HTTP teardown
        if broadcaster is not None:
            await broadcaster.stop()
        # 8c. close all WebRTC peer connections (also drains encoder tasks)
        await shutdown_peer_connections()
        # 8d. stop tick thread BEFORE restoring CARLA (no ticks during restore)
        if tick_loop is not None:
            log.info("stopping tick loop…")
            tick_loop.stop()
            tick_loop.join(timeout=3.0)
        # 8e. scenario teardown (destroys spawned actors + unbinds cameras).
        #     Runs BEFORE camera detach_all so unbind can detach cleanly.
        if scenario_runner is not None:
            try:
                scenario_runner.stop()
            except Exception:
                log.exception("scenario_runner.stop() failed")
        # 8f. detach CARLA camera sensors BEFORE restoring async mode
        try:
            camera_manager.detach_all()
        except Exception:
            log.exception("camera detach_all failed")
        # 8g. drain HTTP / Socket.IO
        if runner is not None:
            await runner.cleanup()
        # 8h. restore CARLA async mode + disconnect (CRITICAL — runs even
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
        return asyncio.run(
            _run(settings, no_carla=args.no_carla, record_dir=args.record_dir)
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(cli())
