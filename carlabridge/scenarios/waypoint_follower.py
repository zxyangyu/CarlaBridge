"""SimpleWaypointFollower — lightweight CARLA path follower.

Drop-in replacement for `BasicAgent` when its per-tick RPC storm
(`bounding_box`, `get_actors().filter('vehicle.*')`, traffic-light reads, …)
deadlocks against our 3-camera sync-mode bridge.

What it does:
- At `set_destination(end_location)`: builds a route ONCE through
  `GlobalRoutePlanner`, stores it as a list of `carla.Location`.
- On `run_step()`: queries ONLY `actor.get_transform()` + `actor.get_velocity()`,
  computes steering as proportional to heading error, throttles to target
  speed. Returns a `carla.VehicleControl`.

What it does NOT do (compared to BasicAgent):
- No obstacle detection (no other actor enumeration)
- No traffic-light handling (UGV happily runs red lights — fine for S1 demo)
- No lane invasion / lateral offset handling

The trade-off is intentional: spec §17 lists this as the fallback because the
demo's "drive to a coord" semantic is the bare minimum needed for AC-5/6.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import carla

log = logging.getLogger(__name__)


# Tunable defaults; all values overridable via constructor.
DEFAULT_TARGET_SPEED_MPS = 25.0 / 3.6   # 25 km/h ≈ 6.94 m/s
DEFAULT_REACH_RADIUS_M = 3.0            # consider waypoint "reached" within 3m
DEFAULT_ARRIVAL_RADIUS_M = 4.0          # final-destination tolerance
DEFAULT_SAMPLING_RES_M = 2.0            # GRP waypoint spacing
DEFAULT_LOOKAHEAD_M = 6.0               # pure-pursuit aim distance; larger → smoother
DEFAULT_HEADING_FULL_LOCK_DEG = 60.0    # heading error that maps to |steer|=1
DEFAULT_STEER_SMOOTHING = 0.6           # α in EMA: steer = α·raw + (1−α)·prev


class SimpleWaypointFollower:
    """Stateless-per-tick path follower. Construct once per dispatch."""

    def __init__(
        self,
        vehicle,
        *,
        target_speed_mps: float = DEFAULT_TARGET_SPEED_MPS,
        reach_radius_m: float = DEFAULT_REACH_RADIUS_M,
        arrival_radius_m: float = DEFAULT_ARRIVAL_RADIUS_M,
        lookahead_m: float = DEFAULT_LOOKAHEAD_M,
        heading_full_lock_deg: float = DEFAULT_HEADING_FULL_LOCK_DEG,
        steer_smoothing: float = DEFAULT_STEER_SMOOTHING,
    ) -> None:
        self._vehicle = vehicle
        self._target_speed = target_speed_mps
        self._reach_r = reach_radius_m
        self._arrival_r = arrival_radius_m
        self._lookahead = lookahead_m
        self._heading_full_lock = heading_full_lock_deg
        self._steer_alpha = steer_smoothing
        self._waypoints: list = []        # list of (x, y, z) tuples
        self._idx = 0
        self._final_xyz: tuple[float, float, float] | None = None
        self._arrived = False
        self._prev_steer = 0.0

    # ---- setup --------------------------------------------------------

    def set_destination(self, world, end_location) -> None:
        """Compute route from current pose to `end_location`. One-shot.

        Uses CARLA's bundled `GlobalRoutePlanner` (same one BasicAgent uses).
        On failure, falls back to a straight-line single-segment route, which
        is good enough if the destination is near the current lane.
        """
        # Import inside the function so unit tests don't need carla.
        from agents.navigation.global_route_planner import GlobalRoutePlanner

        carla_map = world.get_map()
        start_loc = self._vehicle.get_location()
        try:
            grp = GlobalRoutePlanner(carla_map, sampling_resolution=DEFAULT_SAMPLING_RES_M)
            trace = grp.trace_route(start_loc, end_location)
            self._waypoints = [
                (wp.transform.location.x, wp.transform.location.y, wp.transform.location.z)
                for (wp, _opt) in trace
            ]
        except Exception:
            log.exception("WaypointFollower: GRP failed; falling back to direct segment")
            self._waypoints = []
        # Always append the literal destination so the follower targets it
        # even if GRP rounded to lane center.
        self._final_xyz = (end_location.x, end_location.y, end_location.z)
        self._waypoints.append(self._final_xyz)
        self._idx = 0
        self._arrived = False
        log.info(
            "WaypointFollower: route built with %d waypoints (dest=%.1f,%.1f)",
            len(self._waypoints), end_location.x, end_location.y,
        )

    # ---- per-tick API -------------------------------------------------

    def run_step(self):
        """Return `carla.VehicleControl`. Light on RPCs:
        only `get_transform` + `get_velocity`."""
        import carla  # local import; tests stub this via the actor fake

        if self._arrived or not self._waypoints:
            return carla.VehicleControl(throttle=0.0, brake=1.0, steer=0.0)

        tf = self._vehicle.get_transform()
        loc = tf.location

        # Advance past any waypoints already inside the reach radius. This
        # is a tight loop but bounded by the route length, so worst-case is
        # the full route on the first tick of a short hop.
        while self._idx < len(self._waypoints) - 1:
            wx, wy, _ = self._waypoints[self._idx]
            if _xy_distance(loc.x, loc.y, wx, wy) < self._reach_r:
                self._idx += 1
            else:
                break

        # Arrival check uses the literal destination + a generous radius.
        if self._final_xyz is not None:
            fx, fy, _ = self._final_xyz
            if _xy_distance(loc.x, loc.y, fx, fy) < self._arrival_r:
                self._arrived = True
                return carla.VehicleControl(throttle=0.0, brake=1.0, steer=0.0)

        # Pure-pursuit aim: walk forward from _idx until a waypoint is at least
        # `_lookahead` away. Aiming at a distant waypoint keeps the heading
        # error geometrically small under modest lateral offset, which is the
        # main cause of single-tick steer flips at 30 Hz.
        aim_idx = self._idx
        while aim_idx < len(self._waypoints) - 1:
            wx, wy, _ = self._waypoints[aim_idx]
            if _xy_distance(loc.x, loc.y, wx, wy) < self._lookahead:
                aim_idx += 1
            else:
                break

        tx, ty, _ = self._waypoints[aim_idx]
        dx, dy = tx - loc.x, ty - loc.y
        desired_yaw = math.degrees(math.atan2(dy, dx))
        diff = _wrap180(desired_yaw - tf.rotation.yaw)

        # P-controller on heading + EMA low-pass. The EMA absorbs the per-tick
        # sign flips that produce visible jitter; α controls how much new input
        # bleeds through.
        raw_steer = max(-1.0, min(1.0, diff / self._heading_full_lock))
        steer = self._steer_alpha * raw_steer + (1.0 - self._steer_alpha) * self._prev_steer
        self._prev_steer = steer

        # P-controller on speed.
        v = self._vehicle.get_velocity()
        speed = math.sqrt(v.x * v.x + v.y * v.y)  # ignore z (we're on roads)
        if speed < self._target_speed:
            throttle = 0.6
            brake = 0.0
        elif speed > self._target_speed * 1.2:
            throttle = 0.0
            brake = 0.3
        else:
            throttle = 0.3
            brake = 0.0

        return carla.VehicleControl(
            throttle=throttle, brake=brake, steer=steer
        )

    def done(self) -> bool:
        return self._arrived

    @property
    def waypoint_count(self) -> int:
        return len(self._waypoints)

    @property
    def current_index(self) -> int:
        return self._idx


# ---------- math helpers (kept module-level for unit testing) ----------


def _xy_distance(x1: float, y1: float, x2: float, y2: float) -> float:
    dx = x1 - x2
    dy = y1 - y2
    return math.sqrt(dx * dx + dy * dy)


def _wrap180(deg: float) -> float:
    """Wrap angle into [-180, 180]."""
    while deg > 180.0:
        deg -= 360.0
    while deg < -180.0:
        deg += 360.0
    return deg


__all__ = ["SimpleWaypointFollower", "_xy_distance", "_wrap180"]
