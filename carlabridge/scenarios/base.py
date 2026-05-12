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
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:  # pragma: no cover
    import carla

    from carlabridge.core.fleet import Fleet
    from carlabridge.core.world import World
    from carlabridge.obs.event_log import EventLog
    from carlabridge.sensors.camera import CameraManager

log = logging.getLogger(__name__)


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
    ) -> None:
        self.world = world
        self.fleet = fleet
        self.camera_manager = camera_manager
        self.event_log = event_log
        # Caller of `_register_actor` accumulates here; `teardown` destroys
        # everything in this list. Subclasses MUST use this helper to be
        # crash-safe.
        self._spawned_actors: list["carla.Actor"] = []
        self._registered_entity_ids: list[str] = []
        self._rebound_channels: list[str] = []
        self._is_setup = False

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
    "register_scenario",
    "get_scenario_class",
    "available_scenarios",
]
