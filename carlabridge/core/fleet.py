"""Fleet — registry of controllable entities.

Two member kinds:
- CarlaActorMember: wraps a real CARLA actor (e.g. UGV vehicle)
- VirtualMember:    bridge-internal data entity (e.g. UAV with no CARLA actor)

Traffic lights live in `world.get_actors().filter('traffic.traffic_light')`
and are NOT in Fleet — they are read-only world state captured directly by
the SnapshotBuilder (M2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from threading import RLock
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:  # pragma: no cover -- avoid hard import of `carla` at type-check time
    import carla

    from carlabridge.core.incident import Incident

# Role enums. Kept as plain str literals for forward-compat with config / scripts.
Role = Literal[
    "patrol",        # UAV cruising on assigned waypoints
    "follow",        # UAV holding above target
    "standby",       # UAV idle
    "dispatchable",  # UGV that can receive DISPATCH commands
    "civilian",      # background vehicle, not commanded
]


@dataclass(slots=True)
class Pose:
    """Cartesian + Euler (degrees) pose. Units: meters / degrees."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0

    def distance_to(self, other: "Pose") -> float:
        dx = self.x - other.x
        dy = self.y - other.y
        dz = self.z - other.z
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def lerp_toward(self, target: "Pose", max_step: float) -> "Pose":
        """Return a new Pose moved up to `max_step` meters toward `target`.

        If the remaining distance is <= max_step, snaps to target.
        Yaw/pitch/roll are linearly interpolated by fraction-of-distance.
        """
        d = self.distance_to(target)
        if d <= 1e-6 or d <= max_step:
            return Pose(target.x, target.y, target.z, target.yaw, target.pitch, target.roll)
        t = max_step / d
        return Pose(
            x=self.x + (target.x - self.x) * t,
            y=self.y + (target.y - self.y) * t,
            z=self.z + (target.z - self.z) * t,
            yaw=self.yaw + (target.yaw - self.yaw) * t,
            pitch=self.pitch + (target.pitch - self.pitch) * t,
            roll=self.roll + (target.roll - self.roll) * t,
        )


class Member(Protocol):
    entity_id: str
    role: Role

    def pose(self) -> Pose: ...


@dataclass(slots=True)
class CarlaActorMember:
    entity_id: str
    role: Role
    actor: "carla.Actor"

    def pose(self) -> Pose:
        # Lazy access; CARLA actor transform is mutable in-place.
        tf = self.actor.get_transform()
        loc, rot = tf.location, tf.rotation
        return Pose(loc.x, loc.y, loc.z, rot.yaw, rot.pitch, rot.roll)


@dataclass(slots=True)
class VirtualMember:
    """Data-only entity (e.g. virtual UAV). Bridge owns its pose."""

    entity_id: str
    role: Role
    _pose: Pose = field(default_factory=Pose)
    altitude: float = 0.0
    heading: float = 0.0
    battery: float = 100.0
    target: Pose | None = None
    cruise_speed: float = 15.0  # m/s

    def pose(self) -> Pose:
        return self._pose

    def set_pose(self, p: Pose) -> None:
        self._pose = p
        self.altitude = p.z
        self.heading = p.yaw

    def set_target(self, target: Pose | None, cruise_speed: float | None = None) -> None:
        self.target = target
        if cruise_speed is not None:
            self.cruise_speed = cruise_speed

    def step(self, dt: float) -> None:
        """Advance one tick. If target is set, move toward it at cruise_speed.

        Battery drains slowly while in motion or always — keep it simple here:
        constant drain when there's a target; idle otherwise.
        """
        if self.target is None:
            return
        step_m = self.cruise_speed * dt
        next_pose = self._pose.lerp_toward(self.target, step_m)
        self._pose = next_pose
        self.altitude = next_pose.z
        self.heading = next_pose.yaw
        # Arrived?
        if self._pose.distance_to(self.target) < 1e-3:
            self.target = None
        # Drain.
        self.battery = max(0.0, self.battery - 0.05 * dt)


class Fleet:
    """Thread-safe registry of entities indexed by entity_id.

    Refactor v0.3 (design §6.1): also owns
    - ``origins[entity_id] → Pose``: where each entity should return to on
      ``*_RTL``; written by the scenario during ``setup()``.
    - ``incidents[id] → Incident``: active fire events. No ``status`` field —
      membership is itself the active signal (design §4.1).
    """

    def __init__(self) -> None:
        self._members: dict[str, CarlaActorMember | VirtualMember] = {}
        self._origins: dict[str, Pose] = {}
        self._incidents: dict[str, "Incident"] = {}
        self._lock = RLock()

    # ---- registration ---------------------------------------------------

    def register(self, member: CarlaActorMember | VirtualMember) -> None:
        # Origin is NOT written here — scenario.setup decides explicitly.
        with self._lock:
            if member.entity_id in self._members:
                raise ValueError(f"entity_id already registered: {member.entity_id}")
            self._members[member.entity_id] = member

    def unregister(self, entity_id: str) -> CarlaActorMember | VirtualMember | None:
        with self._lock:
            self._origins.pop(entity_id, None)
            return self._members.pop(entity_id, None)

    def clear(self) -> None:
        with self._lock:
            self._members.clear()
            self._origins.clear()
            self._incidents.clear()

    # ---- origins (design §3.2 *_RTL / §5.2 reset) ----------------------

    def set_origin(self, entity_id: str, pose: Pose) -> None:
        with self._lock:
            self._origins[entity_id] = pose

    def get_origin(self, entity_id: str) -> Pose | None:
        with self._lock:
            return self._origins.get(entity_id)

    def origins(self) -> dict[str, Pose]:
        """Snapshot (copy) of the origin map. Safe to iterate without lock."""
        with self._lock:
            return dict(self._origins)

    # ---- incidents (design §4.1) ---------------------------------------

    def add_incident(self, incident: "Incident") -> None:
        with self._lock:
            if incident.id in self._incidents:
                raise ValueError(f"incident_id already exists: {incident.id}")
            self._incidents[incident.id] = incident

    def remove_incident(self, incident_id: str) -> "Incident | None":
        with self._lock:
            return self._incidents.pop(incident_id, None)

    def get_incident(self, incident_id: str) -> "Incident | None":
        with self._lock:
            return self._incidents.get(incident_id)

    def incidents(self) -> dict[str, "Incident"]:
        """Snapshot (copy) of active incidents."""
        with self._lock:
            return dict(self._incidents)

    def clear_incidents(self) -> None:
        with self._lock:
            self._incidents.clear()

    # ---- query ----------------------------------------------------------

    def get(self, entity_id: str) -> CarlaActorMember | VirtualMember | None:
        with self._lock:
            return self._members.get(entity_id)

    def all(self) -> list[CarlaActorMember | VirtualMember]:
        with self._lock:
            return list(self._members.values())

    def by_role(self, role: Role) -> list[CarlaActorMember | VirtualMember]:
        with self._lock:
            return [m for m in self._members.values() if m.role == role]

    def virtual(self) -> list[VirtualMember]:
        with self._lock:
            return [m for m in self._members.values() if isinstance(m, VirtualMember)]

    def carla_members(self) -> list[CarlaActorMember]:
        with self._lock:
            return [m for m in self._members.values() if isinstance(m, CarlaActorMember)]

    def __len__(self) -> int:
        with self._lock:
            return len(self._members)
