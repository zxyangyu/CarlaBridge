"""WorldSnapshot — the single source of truth handed across the sim/async boundary.

Built by `SnapshotBuilder` on the tick thread, published via an `AtomicRef`,
read by the broadcaster (10 Hz) for both projections (frontend / agent).

Field shapes are pinned to spec.md §8 so the agent-side payload stays stable
even when the frontend projector evolves.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Literal

from carlabridge.core.fleet import CarlaActorMember, Fleet, Pose, VirtualMember

if TYPE_CHECKING:  # pragma: no cover
    import carla

log = logging.getLogger(__name__)

Phase = Literal["red", "yellow", "green", "off", "unknown"]


# ---------- entity rows ---------------------------------------------------


@dataclass(slots=True)
class TrafficLightState:
    id: str
    pose: tuple[float, float, float]  # x, y, z (CARLA world meters)
    phase: Phase
    remaining_s: float  # seconds left in current phase (best-effort)


@dataclass(slots=True)
class VehicleState:
    id: str
    role: str  # 'dispatchable' | 'civilian'
    pose: tuple[float, float, float]
    yaw: float
    speed: float  # m/s
    heading: float  # degrees, alias of yaw for spec parity
    battery: float | None = None  # None for civilian vehicles


@dataclass(slots=True)
class UavState:
    id: str
    role: str  # 'patrol' | 'follow' | 'standby'
    pose: tuple[float, float, float]
    altitude: float
    heading: float
    battery: float
    speed: float = 0.0


# ---------- the snapshot ---------------------------------------------------


@dataclass(slots=True)
class WorldSnapshot:
    sim_time: float
    traffic_lights: list[TrafficLightState] = field(default_factory=list)
    vehicles: list[VehicleState] = field(default_factory=list)
    uavs: list[UavState] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-ready full payload — used directly by `for_agent`."""
        return asdict(self)


# ---------- builder -------------------------------------------------------


_PHASE_MAP: dict[str, Phase] = {
    "Red": "red",
    "Yellow": "yellow",
    "Green": "green",
    "Off": "off",
    "Unknown": "unknown",
}


def _pose_tuple(p: Pose) -> tuple[float, float, float]:
    return (p.x, p.y, p.z)


def _vel_magnitude(actor: "carla.Actor") -> float:
    try:
        v = actor.get_velocity()
        return math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)
    except Exception:
        return 0.0


def _traffic_light_phase(light: "carla.TrafficLight") -> tuple[Phase, float]:
    """Map CARLA state → spec phase + best-effort remaining seconds.

    CARLA exposes `get_red_time/get_green_time/get_yellow_time/get_elapsed_time`.
    We compute remaining = phase_duration - elapsed_in_phase.
    """
    try:
        state_name = str(light.get_state()).rsplit(".", 1)[-1]  # 'TrafficLightState.Red' → 'Red'
        phase = _PHASE_MAP.get(state_name, "unknown")
    except Exception:
        return ("unknown", 0.0)
    try:
        elapsed = float(light.get_elapsed_time())
        if phase == "red":
            total = float(light.get_red_time())
        elif phase == "yellow":
            total = float(light.get_yellow_time())
        elif phase == "green":
            total = float(light.get_green_time())
        else:
            total = 0.0
        remaining = max(0.0, total - elapsed)
    except Exception:
        remaining = 0.0
    return (phase, remaining)


class SnapshotBuilder:
    """Reads world + fleet on the tick thread and produces a WorldSnapshot.

    Stateless except for a cached traffic-light actor list — CARLA traffic lights
    are stable for the lifetime of the map, so we refresh the cache lazily and
    only re-scan when the cache is empty or `refresh_lights()` is called.
    """

    def __init__(self, world: "object | None") -> None:
        # `world` typed loosely so tests can pass a FakeWorld.
        self._world = world
        self._tl_cache: list["carla.TrafficLight"] = []

    # ---- public API ----------------------------------------------------

    def refresh_lights(self) -> None:
        """Drop the traffic-light cache; next build() rescans."""
        self._tl_cache = []

    def build(self, fleet: Fleet, sim_time: float) -> WorldSnapshot:
        return WorldSnapshot(
            sim_time=sim_time,
            traffic_lights=self._read_traffic_lights(),
            vehicles=self._read_vehicles(fleet),
            uavs=self._read_uavs(fleet),
        )

    # ---- internals -----------------------------------------------------

    def _read_traffic_lights(self) -> list[TrafficLightState]:
        if self._world is None:
            return []
        if not self._tl_cache:
            try:
                actors = self._world.get_actors().filter("traffic.traffic_light")
                self._tl_cache = list(actors)
            except Exception as e:
                log.debug("traffic light scan failed: %s", e)
                return []
        out: list[TrafficLightState] = []
        for light in self._tl_cache:
            try:
                loc = light.get_location()
                phase, remaining = _traffic_light_phase(light)
                out.append(
                    TrafficLightState(
                        id=f"TL-{light.id}",
                        pose=(loc.x, loc.y, loc.z),
                        phase=phase,
                        remaining_s=round(remaining, 2),
                    )
                )
            except Exception:
                continue
        return out

    def _read_vehicles(self, fleet: Fleet) -> list[VehicleState]:
        out: list[VehicleState] = []
        for m in fleet.carla_members():
            try:
                p = m.pose()
                speed = _vel_magnitude(m.actor)
                out.append(
                    VehicleState(
                        id=m.entity_id,
                        role=m.role,
                        pose=_pose_tuple(p),
                        yaw=p.yaw,
                        speed=round(speed, 3),
                        heading=p.yaw,
                        battery=None,
                    )
                )
            except Exception as e:
                log.debug("vehicle read failed for %s: %s", m.entity_id, e)
                continue
        return out

    def _read_uavs(self, fleet: Fleet) -> list[UavState]:
        out: list[UavState] = []
        for m in fleet.virtual():
            p = m.pose()
            speed = m.cruise_speed if m.target is not None else 0.0
            out.append(
                UavState(
                    id=m.entity_id,
                    role=m.role,
                    pose=_pose_tuple(p),
                    altitude=m.altitude,
                    heading=m.heading,
                    battery=m.battery,
                    speed=speed,
                )
            )
        return out


__all__ = [
    "Phase",
    "TrafficLightState",
    "VehicleState",
    "UavState",
    "WorldSnapshot",
    "SnapshotBuilder",
]
