"""FakeWorld + fake CARLA actors for testing without a real CARLA server.

Implements the minimum surface SnapshotBuilder needs:
- get_actors().filter('traffic.traffic_light') → list of FakeTrafficLight
- each light has: id, get_location(), get_state(), get_red_time/get_yellow_time/
  get_green_time/get_elapsed_time

For vehicle members: FakeActor exposing get_transform() + get_velocity().
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class FakeLocation:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


@dataclass(slots=True)
class FakeRotation:
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0


@dataclass(slots=True)
class FakeTransform:
    location: FakeLocation
    rotation: FakeRotation


@dataclass(slots=True)
class FakeVelocity:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class FakeActor:
    """Stand-in for a CARLA vehicle actor."""

    def __init__(
        self,
        actor_id: int,
        x: float = 0.0,
        y: float = 0.0,
        z: float = 0.0,
        yaw: float = 0.0,
        vx: float = 0.0,
        vy: float = 0.0,
    ) -> None:
        self.id = actor_id
        self._tf = FakeTransform(FakeLocation(x, y, z), FakeRotation(yaw, 0, 0))
        self._v = FakeVelocity(vx, vy, 0.0)

    def get_transform(self) -> FakeTransform:
        return self._tf

    def get_velocity(self) -> FakeVelocity:
        return self._v

    def set_pose(self, x: float, y: float, z: float, yaw: float = 0.0) -> None:
        self._tf = FakeTransform(FakeLocation(x, y, z), FakeRotation(yaw, 0, 0))


class FakeTrafficLightStateEnum:
    """str() of a CARLA enum looks like 'TrafficLightState.Red'; mimic that."""

    def __init__(self, name: str):
        self._name = name

    def __str__(self) -> str:
        return f"TrafficLightState.{self._name}"


class FakeTrafficLight:
    def __init__(
        self,
        actor_id: int,
        x: float,
        y: float,
        z: float,
        state: str = "Red",
        red_time: float = 10.0,
        yellow_time: float = 3.0,
        green_time: float = 12.0,
        elapsed: float = 4.0,
    ) -> None:
        self.id = actor_id
        self._loc = FakeLocation(x, y, z)
        self._state = FakeTrafficLightStateEnum(state)
        self._red = red_time
        self._yellow = yellow_time
        self._green = green_time
        self._elapsed = elapsed

    def get_location(self) -> FakeLocation:
        return self._loc

    def get_state(self) -> FakeTrafficLightStateEnum:
        return self._state

    def get_red_time(self) -> float:
        return self._red

    def get_yellow_time(self) -> float:
        return self._yellow

    def get_green_time(self) -> float:
        return self._green

    def get_elapsed_time(self) -> float:
        return self._elapsed


class FakeActorList:
    """Stand-in for the result of world.get_actors() — supports .filter(blueprint)."""

    def __init__(self, actors: list[object]) -> None:
        self._actors = actors

    def filter(self, blueprint_pattern: str) -> list[object]:
        # Only `traffic.traffic_light` is exercised by SnapshotBuilder today.
        if blueprint_pattern == "traffic.traffic_light":
            return [a for a in self._actors if isinstance(a, FakeTrafficLight)]
        return []

    def __iter__(self):
        return iter(self._actors)


class FakeWorld:
    """World stub for SnapshotBuilder. Add actors via add_traffic_light / add_actor."""

    def __init__(self) -> None:
        self._actors: list[object] = []
        self.tick_calls = 0

    # --- builder surface --------------------------------------------------

    def add_traffic_light(self, light: FakeTrafficLight) -> None:
        self._actors.append(light)

    def add_actor(self, actor: FakeActor) -> None:
        self._actors.append(actor)

    # --- CARLA-like surface ----------------------------------------------

    def get_actors(self) -> FakeActorList:
        return FakeActorList(self._actors)

    def tick(self) -> int:
        self.tick_calls += 1
        return self.tick_calls
