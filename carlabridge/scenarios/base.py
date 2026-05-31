"""Scenario base class + registry.

Lifecycle:
    scenario = ScenarioClass(world=, fleet=, camera_manager=, event_log=)
    scenario.setup()                        # spawn actors, register fleet, rebind cameras
    # ... tick loop runs, calls on_tick_pre/post and on_command per tick ...
    scenario.teardown()                     # destroy spawned actors

`setup()` runs on the MAIN thread (before the tick thread starts). All
on_tick_* and on_command callbacks run on the TICK thread. `teardown()` runs
on the main thread again (called by the lifecycle owner after tick stop).

Registry pattern:
    @register_scenario("s1_fire")
    class S1FireScenario(Scenario):
        ...

    scenario_cls = get_scenario_class("s1_fire")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from carlabridge.commands.enum import CommandKind, ParsedCommand
from carlabridge.scenarios.in_flight import Awaiting, InFlightCommand

if TYPE_CHECKING:  # pragma: no cover
    import carla

    from carlabridge.commands.bus import CommandBus
    from carlabridge.core.fleet import Fleet
    from carlabridge.core.world import World
    from carlabridge.obs.event_log import EventLog
    from carlabridge.sensors.camera import CameraManager

log = logging.getLogger(__name__)


# ---- Completion outcome ---------------------------------------------------


CompletionStatus = Literal["completed", "failed", "cancelled"]


@dataclass(slots=True, frozen=True)
class CompletionResult:
    """Outcome of an in-flight command (design §3.4).

    ``completed`` carries no reason. ``failed`` / ``cancelled`` carry a
    machine-readable ``reason`` (design §3.4 enums).
    """

    status: CompletionStatus
    reason: str | None = None
    detail: dict | None = None

    @classmethod
    def completed(cls) -> "CompletionResult":
        return cls(status="completed")

    @classmethod
    def failed(cls, reason: str, detail: dict | None = None) -> "CompletionResult":
        return cls(status="failed", reason=reason, detail=detail)

    @classmethod
    def cancelled(cls, reason: str, detail: dict | None = None) -> "CompletionResult":
        return cls(status="cancelled", reason=reason, detail=detail)


class Scenario:
    """Base class. Override `setup` and `teardown`; override hooks as needed."""

    name: ClassVar[str] = "unnamed"

    def __init__(
        self,
        *,
        world: "World",
        fleet: "Fleet",
        camera_manager: "CameraManager",
        event_log: "EventLog",
        command_bus: "CommandBus | None" = None,
        settings: Any | None = None,
    ) -> None:
        self.world = world
        self.fleet = fleet
        self.camera_manager = camera_manager
        self.event_log = event_log
        self.settings = settings
        # Refactor v0.3 — wired through by the ScenarioRunner once R6 lands.
        # None during R3 unit tests, which exercise the lifecycle in isolation.
        self.command_bus = command_bus
        # Caller of `_register_actor` accumulates here; `teardown` destroys
        # everything in this list. Subclasses MUST use this helper to be
        # crash-safe.
        self._spawned_actors: list["carla.Actor"] = []
        self._registered_entity_ids: list[str] = []
        self._rebound_channels: list[str] = []
        self._is_setup = False
        # Refactor v0.3 — in-flight command bookkeeping (design §6.1).
        # Indexed twice so supersede stays O(1).
        self._in_flight: dict[str, InFlightCommand] = {}
        self._in_flight_by_entity: dict[str, str] = {}
        self._run_id: int = 0

    # ---- spawn helpers (use these from `setup` so teardown is automatic) -

    def _register_actor(self, actor: "carla.Actor") -> "carla.Actor":
        self._spawned_actors.append(actor)
        return actor

    def _register_entity(self, entity_id: str) -> None:
        self._registered_entity_ids.append(entity_id)

    def _record_rebound(self, channel_id: str) -> None:
        self._rebound_channels.append(channel_id)

    # ---- lifecycle hooks (override) ----------------------------------

    def setup(self) -> None:
        """Called once on main thread. Spawn actors, register fleet, set bindings."""

    def on_tick_pre(self, sim_time: float) -> None:
        """Called from tick thread BEFORE world.tick()."""

    def on_tick_post(self, sim_time: float) -> None:
        """Called from tick thread AFTER world.tick() + snapshot build."""

    def on_command(self, cmd: Any) -> None:
        """Called from tick thread when a command is dequeued (M6 wires)."""

    # ---- in-flight command lifecycle (design §3.4, §6.4) -------------

    def in_flight_snapshot(self) -> list[dict]:
        """Wire-shape list for ``state.snapshot.payload.in_flight_commands``.

        Stable sort by ``accepted_at_sim_time`` so wire diffs stay readable.
        """
        items = sorted(
            self._in_flight.values(), key=lambda i: i.accepted_at_sim_time
        )
        return [i.to_snapshot_entry() for i in items]

    def _accept_command(
        self,
        cmd: ParsedCommand,
        sim_time: float,
        awaiting: Awaiting,
    ) -> InFlightCommand:
        """Register a freshly-accepted command. If the target entity has a
        prior in-flight command, that command is finalised as ``cancelled``
        first (design §6.4 supersede).

        ``UGV_STOP`` carries the special ``explicit_stop`` reason on the
        superseded sibling; every other kind uses ``superseded``.
        """
        old_cmd_id = self._in_flight_by_entity.get(cmd.target)
        if old_cmd_id is not None and old_cmd_id in self._in_flight:
            old = self._in_flight[old_cmd_id]
            reason = (
                "explicit_stop"
                if cmd.kind == CommandKind.UGV_STOP
                else "superseded"
            )
            self._finalize_command(
                old_cmd_id, old,
                CompletionResult.cancelled(reason, {"by_cmd_id": cmd.id}),
                sim_time,
            )
        in_flt = InFlightCommand(
            cmd_id=cmd.id,
            kind=cmd.kind,
            target=cmd.target,
            params=dict(cmd.params),
            accepted_at_sim_time=sim_time,
            awaiting=awaiting,
        )
        self._in_flight[cmd.id] = in_flt
        self._in_flight_by_entity[cmd.target] = cmd.id
        return in_flt

    def _check_completion(
        self, in_flt: InFlightCommand, sim_time: float
    ) -> CompletionResult | None:
        """Resolve whether ``in_flt`` is done this tick (design §6.2).

        Base provides the two trivial branches:

        * ``awaiting == "instant"`` → :meth:`CompletionResult.completed`
        * ``awaiting == "ongoing"`` → None (never completes naturally; only
          supersede / reset / shutdown clear it)

        Subclasses extend for arrival / extinguish / patrol_finish.
        """
        if in_flt.awaiting == "instant":
            return CompletionResult.completed()
        return None

    def _finalize_command(
        self,
        cmd_id: str,
        in_flt: InFlightCommand,
        result: CompletionResult,
        sim_time: float,
    ) -> None:
        """Move ``cmd_id`` out of the in-flight indexes and broadcast its
        terminal status (command_status + event_log).
        """
        self._in_flight.pop(cmd_id, None)
        if self._in_flight_by_entity.get(in_flt.target) == cmd_id:
            self._in_flight_by_entity.pop(in_flt.target, None)
        payload = {
            "cmd_id": cmd_id,
            "status": result.status,
            "kind": in_flt.kind.value,
            "target": in_flt.target,
            "reason": result.reason,
            "detail": result.detail,
            "at_sim_time": sim_time,
        }
        if self.command_bus is not None:
            self.command_bus.broadcast_command_status(payload)
            self.command_bus.forget(cmd_id)
        severity_map = {
            "completed": "ok",
            "failed": "danger",
            "cancelled": "info",
        }
        sev = severity_map[result.status]
        msg = (
            f"{result.status} {cmd_id} target={in_flt.target} "
            f"kind={in_flt.kind.value}"
        )
        if result.reason:
            msg += f" reason={result.reason}"
        self.event_log.add(sev, "SCENARIO", msg, cmd_id=cmd_id)

    def _drive_command_lifecycle(self, sim_time: float) -> None:
        """Scan ``_in_flight`` and finalise any command whose
        ``_check_completion`` returns a non-None result this tick.

        Snapshot the keys before iterating so ``_finalize_command`` can mutate
        the dict mid-loop.
        """
        if not self._in_flight:
            return
        for cmd_id in list(self._in_flight.keys()):
            in_flt = self._in_flight.get(cmd_id)
            if in_flt is None:
                continue
            result = self._check_completion(in_flt, sim_time)
            if result is not None:
                self._finalize_command(cmd_id, in_flt, result, sim_time)

    def _cancel_all_in_flight(
        self, reason: str, sim_time: float, detail: dict | None = None
    ) -> list[str]:
        """Finalise every in-flight command as ``cancelled(reason=...)``.

        Used by ``reset()`` (R4-04) and ``bridge_shutdown`` paths. Returns
        the list of cancelled ``cmd_id``s for the HTTP response body.
        """
        cancelled: list[str] = []
        for cmd_id in list(self._in_flight.keys()):
            in_flt = self._in_flight.get(cmd_id)
            if in_flt is None:
                continue
            self._finalize_command(
                cmd_id, in_flt,
                CompletionResult.cancelled(reason, detail),
                sim_time,
            )
            cancelled.append(cmd_id)
        return cancelled

    def teardown(self) -> None:
        """Default: destroy every actor registered via _register_actor, drop
        every entity registered via _register_entity, rebind every recorded
        channel back to None. Override AFTER calling super().teardown() to
        run extra cleanup.
        """
        # CARLA actors first.
        for actor in reversed(self._spawned_actors):
            try:
                actor.destroy()
            except Exception:  # pragma: no cover -- best-effort
                log.exception("scenario teardown: actor.destroy() failed")
        self._spawned_actors.clear()
        # Fleet entries.
        for entity_id in self._registered_entity_ids:
            try:
                self.fleet.unregister(entity_id)
            except Exception:  # pragma: no cover
                log.exception("scenario teardown: fleet.unregister(%s) failed", entity_id)
        self._registered_entity_ids.clear()
        # Cameras — unbind so the next scenario starts clean.
        for channel_id in self._rebound_channels:
            try:
                self.camera_manager.rebind(
                    channel_id,
                    None,
                    world=self.world.carla_world,
                    fleet=self.fleet,
                )
            except Exception:  # pragma: no cover
                log.exception("scenario teardown: rebind(%s, None) failed", channel_id)
        self._rebound_channels.clear()


# ---------- registry -----------------------------------------------------


_REGISTRY: dict[str, type[Scenario]] = {}


def register_scenario(name: str):
    """Class decorator: `@register_scenario("s1_fire")`."""

    def decorator(cls: type[Scenario]) -> type[Scenario]:
        if name in _REGISTRY:
            log.warning("scenario %r already registered; replacing", name)
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_scenario_class(name: str) -> type[Scenario]:
    if name not in _REGISTRY:
        avail = sorted(_REGISTRY) or ["<none>"]
        raise KeyError(f"unknown scenario: {name!r}. Registered: {avail}")
    return _REGISTRY[name]


def available_scenarios() -> list[str]:
    return sorted(_REGISTRY)


__all__ = [
    "Scenario",
    "CompletionResult",
    "CompletionStatus",
    "register_scenario",
    "get_scenario_class",
    "available_scenarios",
]
