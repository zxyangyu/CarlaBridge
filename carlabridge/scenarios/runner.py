"""ScenarioRunner — owns one Scenario's lifecycle.

M5 scope (current):
- construct from class + deps
- start() runs scenario.setup() on the caller's thread (main, pre-tick-thread)
- stop() runs scenario.teardown()
- exposes `state` and `sim_time_provider` (the latter for M6 mock_agent_loop).

M6 will extend with:
- async start of `scenario.mock_agent_loop(link)` (cancellable task)
- failure propagation so the broadcaster can publish `event_log` danger
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable, Literal

from carlabridge.scenarios.base import Scenario

if TYPE_CHECKING:  # pragma: no cover
    from carlabridge.agent.link import AgentLink
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
    ) -> None:
        self._scenario_cls = scenario_cls
        self._world = world
        self._fleet = fleet
        self._camera_manager = camera_manager
        self._event_log = event_log
        self._sim_time = sim_time_provider
        self._state: State = "idle"
        self._scenario: Scenario | None = None
        self._failure: BaseException | None = None
        self._mock_task: asyncio.Task | None = None

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
        )
        # Inject sim_time provider for scenarios that gate on sim_time
        # (e.g. S1's mock_agent_loop).
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

    # ---- mock-agent task lifecycle (M6) --------------------------------

    def start_mock_agent_task(self, link: "AgentLink") -> asyncio.Task | None:
        """Spawn `scenario.mock_agent_loop(link)` as an asyncio task.

        No-op if the scenario doesn't define `mock_agent_loop`. Idempotent —
        repeat calls return the existing task.
        """
        if self._scenario is None:
            return None
        if self._mock_task is not None and not self._mock_task.done():
            return self._mock_task
        if not hasattr(self._scenario, "mock_agent_loop"):
            return None
        coro = self._scenario.mock_agent_loop(link)
        self._mock_task = asyncio.create_task(
            coro, name=f"mock-agent-{self._scenario_cls.name}"
        )
        return self._mock_task

    async def stop_mock_agent_task(self) -> None:
        if self._mock_task is None:
            return
        if not self._mock_task.done():
            self._mock_task.cancel()
        try:
            await self._mock_task
        except (asyncio.CancelledError, Exception):
            pass
        self._mock_task = None

    @property
    def mock_task(self) -> asyncio.Task | None:
        return self._mock_task

    @property
    def failure(self) -> BaseException | None:
        return self._failure


__all__ = ["ScenarioRunner", "State"]
