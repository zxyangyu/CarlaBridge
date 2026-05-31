"""ScenarioRunner — owns one Scenario's lifecycle.

Responsibilities:

* construct from a registered scenario class + dependencies
* :meth:`start` runs ``scenario.setup`` on the caller's thread (main,
  pre-tick-thread)
* :meth:`stop` runs ``scenario.teardown``
* :meth:`run_in_sim_domain` lets HTTP routes hop into the tick thread and
  await the result on the calling event loop
"""

from __future__ import annotations

import asyncio
import logging
import queue
from typing import TYPE_CHECKING, Any, Callable, Literal

from carlabridge.scenarios.base import Scenario

if TYPE_CHECKING:  # pragma: no cover
    from carlabridge.commands.bus import CommandBus
    from carlabridge.core.fleet import Fleet
    from carlabridge.core.world import World
    from carlabridge.obs.event_log import EventLog
    from carlabridge.sensors.camera import CameraManager

log = logging.getLogger(__name__)

State = Literal["idle", "starting", "running", "stopping", "stopped", "failed"]


class ScenarioRunner:
    """Lightweight lifecycle wrapper. Holds one scenario instance."""

    def __init__(
        self,
        scenario_cls: type[Scenario],
        *,
        world: "World",
        fleet: "Fleet",
        camera_manager: "CameraManager",
        event_log: "EventLog",
        sim_time_provider: Callable[[], float] = lambda: 0.0,
        command_bus: "CommandBus | None" = None,
        settings: Any | None = None,
    ) -> None:
        self._scenario_cls = scenario_cls
        self._world = world
        self._fleet = fleet
        self._camera_manager = camera_manager
        self._event_log = event_log
        self._sim_time = sim_time_provider
        self._command_bus = command_bus
        self._settings = settings
        self._state: State = "idle"
        self._scenario: Scenario | None = None
        self._failure: BaseException | None = None
        # R6-01 cross-domain task queue: HTTP routes push callables here and
        # await an asyncio.Future; the tick thread drains the queue once per
        # tick (TickLoop calls :meth:`drain_sim_tasks`).
        self._sim_tasks: queue.Queue[tuple[Callable[[], Any], asyncio.Future, asyncio.AbstractEventLoop]] = queue.Queue()

    # ---- accessors ----------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    @property
    def scenario(self) -> Scenario | None:
        return self._scenario

    @property
    def name(self) -> str:
        return self._scenario_cls.name

    def sim_time(self) -> float:
        return self._sim_time()

    # ---- lifecycle ----------------------------------------------------

    def start(self) -> Scenario:
        """Construct + setup. Returns the scenario for handoff to TickLoop."""
        if self._state not in ("idle", "stopped", "failed"):
            raise RuntimeError(f"ScenarioRunner.start() called in state={self._state}")
        self._state = "starting"
        self._scenario = self._scenario_cls(
            world=self._world,
            fleet=self._fleet,
            camera_manager=self._camera_manager,
            event_log=self._event_log,
            command_bus=self._command_bus,
            settings=self._settings,
        )
        # Inject sim_time provider so the scenario can stamp incidents /
        # in-flight commands with the current sim_time.
        if hasattr(self._scenario, "attach_sim_time_provider"):
            self._scenario.attach_sim_time_provider(self._sim_time)
        try:
            self._scenario.setup()
            self._scenario._is_setup = True
        except Exception as e:
            log.exception("scenario %s setup failed", self._scenario_cls.name)
            self._failure = e
            self._state = "failed"
            # Best-effort teardown to clean any partial spawn.
            try:
                self._scenario.teardown()
            except Exception:  # pragma: no cover
                log.exception("teardown after failed setup also raised")
            raise
        self._state = "running"
        self._event_log.add(
            "ok", "SCENARIO", f"scenario {self._scenario_cls.name} started"
        )
        log.info("scenario %s started", self._scenario_cls.name)
        return self._scenario

    def stop(self) -> None:
        if self._scenario is None or self._state in ("idle", "stopped"):
            return
        self._state = "stopping"
        try:
            self._scenario.teardown()
            self._event_log.add(
                "ok", "SCENARIO", f"scenario {self._scenario_cls.name} stopped"
            )
        except Exception as e:
            log.exception("scenario %s teardown failed", self._scenario_cls.name)
            self._failure = e
            self._state = "failed"
            return
        self._state = "stopped"

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    # ---- cross-domain task primitive (R6-01) --------------------------

    def run_in_sim_domain(
        self, fn: Callable[..., Any], *args, **kwargs
    ) -> asyncio.Future:
        """Schedule ``fn(*args, **kwargs)`` to run on the tick (sim) thread
        and return an asyncio.Future that resolves on the calling event loop.

        Must be called from async-domain code (HTTP handlers). The future
        resolves on the **same loop** the call happened on, so awaiting it is
        safe.

        Exceptions raised by ``fn`` are propagated via
        :meth:`asyncio.Future.set_exception`.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._sim_tasks.put_nowait((lambda: fn(*args, **kwargs), future, loop))
        return future

    def drain_sim_tasks(self) -> None:
        """Drain the run_in_sim_domain queue. Called by the tick thread once
        per tick; never raises (each task error becomes future.set_exception)."""
        while True:
            try:
                task, future, loop = self._sim_tasks.get_nowait()
            except queue.Empty:
                return
            try:
                result = task()
            except BaseException as e:  # noqa: BLE001 — propagate to async side
                log.exception("sim task raised; routing to future.set_exception")
                if not future.done():
                    loop.call_soon_threadsafe(future.set_exception, e)
            else:
                if not future.done():
                    loop.call_soon_threadsafe(future.set_result, result)

    def is_resetting(self) -> bool:
        """Read-through to scenario._resetting for the namespaces / routes
        that need to gate on it without owning a scenario reference."""
        if self._scenario is None:
            return False
        return bool(getattr(self._scenario, "_resetting", False))


__all__ = ["ScenarioRunner", "State"]
