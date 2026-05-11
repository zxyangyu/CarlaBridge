"""Process entrypoint.

M0: HTTP + Socket.IO skeleton.
M1: + CARLA connection, sync mode, tick thread (NoopScenario placeholder).

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

from carlabridge.bus.server import build_app
from carlabridge.config import Settings, load_settings
from carlabridge.core.clock import SimClock
from carlabridge.core.fleet import Fleet
from carlabridge.core.tick_loop import NoopScenario, TickLoop
from carlabridge.core.world import BridgeFatal, World
from carlabridge.obs.event_log import EventLog
from carlabridge.obs.metrics import Metrics

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

    world: World | None = None
    tick_loop: TickLoop | None = None
    runner: web.AppRunner | None = None
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
        app, _sio = build_app(settings, event_log, metrics, shutdown_event=stop_event)
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

        # ---------- 3. tick thread (NoopScenario for M1) ----------------
        if world is not None:
            clock = SimClock(delta=settings.carla.fixed_delta_seconds)
            tick_loop = TickLoop(
                world=world,
                clock=clock,
                fleet=fleet,
                scenario=NoopScenario(),
                metrics=metrics,
                event_log=event_log,
            )
            tick_loop.start()
            event_log.add("ok", "BRIDGE", "tick loop running (NoopScenario)")

        # ---------- 4. wait for shutdown --------------------------------
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
        # 5a. stop tick thread BEFORE restoring CARLA (no ticks during restore)
        if tick_loop is not None:
            log.info("stopping tick loop…")
            tick_loop.stop()
            tick_loop.join(timeout=3.0)
        # 5b. drain HTTP / Socket.IO
        if runner is not None:
            await runner.cleanup()
        # 5c. restore CARLA async mode + disconnect (CRITICAL — runs even
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
