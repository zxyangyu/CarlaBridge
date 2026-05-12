"""SimpleWaypointFollower tests with mocked carla.

We replace `carla.VehicleControl` with a recording stand-in via monkeypatch.
The follower's `set_destination` path (which calls GlobalRoutePlanner) is
NOT exercised — that needs a real CARLA. We test the per-tick steering /
throttle / arrival logic by pre-seeding the waypoint list directly.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from carlabridge.scenarios.waypoint_follower import (
    SimpleWaypointFollower,
    _wrap180,
    _xy_distance,
)


# ---------- helpers / fakes ------------------------------------------------


@dataclass
class FakeLocation:
    x: float
    y: float
    z: float = 0.0


@dataclass
class FakeRotation:
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0


@dataclass
class FakeTransform:
    location: FakeLocation
    rotation: FakeRotation


@dataclass
class FakeVelocity:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class FakeUgvActor:
    def __init__(self, x: float = 0.0, y: float = 0.0, yaw: float = 0.0,
                 vx: float = 0.0, vy: float = 0.0) -> None:
        self._tf = FakeTransform(FakeLocation(x, y, 0.0), FakeRotation(yaw))
        self._v = FakeVelocity(vx, vy, 0.0)

    def get_transform(self) -> FakeTransform:
        return self._tf

    def get_velocity(self) -> FakeVelocity:
        return self._v

    def get_location(self) -> FakeLocation:
        return self._tf.location

    def set_pose(self, x: float, y: float, yaw: float = 0.0) -> None:
        self._tf = FakeTransform(FakeLocation(x, y, 0.0), FakeRotation(yaw))

    def set_velocity(self, vx: float, vy: float) -> None:
        self._v = FakeVelocity(vx, vy, 0.0)


@dataclass
class FakeControl:
    """Stand-in for carla.VehicleControl."""

    throttle: float = 0.0
    brake: float = 0.0
    steer: float = 0.0


class _FakeCarlaModule:
    """Minimal module surface used by SimpleWaypointFollower.run_step."""

    VehicleControl = FakeControl


@pytest.fixture(autouse=True)
def _patch_carla(monkeypatch):
    """Replace `import carla` inside waypoint_follower with our fake."""
    import sys

    monkeypatch.setitem(sys.modules, "carla", _FakeCarlaModule)
    yield


def _follower_with_waypoints(actor: FakeUgvActor, waypoints, *, final_xyz=None):
    """Bypass set_destination — directly seed the route for testing run_step."""
    f = SimpleWaypointFollower(actor, target_speed_mps=5.0, reach_radius_m=3.0)
    f._waypoints = list(waypoints)
    f._final_xyz = final_xyz if final_xyz is not None else waypoints[-1]
    return f


# ---------- math helpers -------------------------------------------------


def test_xy_distance():
    assert _xy_distance(0, 0, 3, 4) == pytest.approx(5.0)
    assert _xy_distance(1, 1, 1, 1) == 0.0


def test_wrap180_basics():
    assert _wrap180(0) == 0
    assert _wrap180(180) == 180
    assert _wrap180(181) == -179
    assert _wrap180(-181) == 179
    assert _wrap180(370) == 10
    assert _wrap180(-370) == -10


# ---------- steering -----------------------------------------------------


def test_run_step_steers_toward_waypoint_in_front():
    """UGV at origin facing +x, waypoint at (10, 0). Steer should be ~0."""
    actor = FakeUgvActor(x=0, y=0, yaw=0)
    f = _follower_with_waypoints(actor, [(10.0, 0.0, 0.0)])
    ctrl = f.run_step()
    assert abs(ctrl.steer) < 0.05
    assert ctrl.throttle > 0  # need to accelerate
    assert ctrl.brake == 0


def test_run_step_steers_left_for_left_waypoint():
    """UGV facing +x, waypoint to the left (north in CARLA's left-handed yaw):
    waypoint at (0, -10) means target yaw -90°, error = -90° → steer = -1 (full left)."""
    actor = FakeUgvActor(x=0, y=0, yaw=0)
    f = _follower_with_waypoints(actor, [(0.0, -10.0, 0.0)])
    ctrl = f.run_step()
    assert ctrl.steer < -0.5  # strongly negative


def test_run_step_steers_right_for_right_waypoint():
    """Waypoint at (0, +10): yaw 90° error → steer +1."""
    actor = FakeUgvActor(x=0, y=0, yaw=0)
    f = _follower_with_waypoints(actor, [(0.0, 10.0, 0.0)])
    ctrl = f.run_step()
    assert ctrl.steer > 0.5


# ---------- speed control ------------------------------------------------


def test_run_step_throttles_when_slow():
    actor = FakeUgvActor(x=0, y=0, yaw=0, vx=0.0, vy=0.0)
    f = _follower_with_waypoints(actor, [(10.0, 0.0, 0.0)])
    ctrl = f.run_step()
    assert ctrl.throttle > 0
    assert ctrl.brake == 0


def test_run_step_brakes_when_too_fast():
    actor = FakeUgvActor(x=0, y=0, yaw=0, vx=20.0, vy=0.0)  # 20 m/s ≫ 5 target
    f = _follower_with_waypoints(actor, [(50.0, 0.0, 0.0)])
    ctrl = f.run_step()
    assert ctrl.brake > 0
    assert ctrl.throttle == 0


# ---------- waypoint advancement ----------------------------------------


def test_waypoint_index_advances_when_within_reach():
    actor = FakeUgvActor(x=0, y=0, yaw=0)
    waypoints = [(2.0, 0.0, 0.0), (10.0, 0.0, 0.0), (20.0, 0.0, 0.0)]
    f = _follower_with_waypoints(actor, waypoints, final_xyz=waypoints[-1])
    # Start at origin; first waypoint at (2,0) is within reach_radius=3 → skip.
    f.run_step()
    assert f.current_index == 1


def test_arrival_triggers_brake_and_done_flag():
    actor = FakeUgvActor(x=10, y=0, yaw=0)
    f = _follower_with_waypoints(
        actor, [(10.0, 0.0, 0.0)], final_xyz=(10.0, 0.0, 0.0)
    )
    ctrl = f.run_step()
    assert f.done()
    assert ctrl.brake == 1.0
    assert ctrl.throttle == 0.0


def test_run_step_after_arrival_returns_brake():
    actor = FakeUgvActor(x=10, y=0)
    f = _follower_with_waypoints(actor, [(10.0, 0.0, 0.0)])
    f.run_step()  # arrived
    ctrl = f.run_step()
    assert ctrl.brake == 1.0


def test_empty_waypoints_returns_brake():
    actor = FakeUgvActor()
    f = SimpleWaypointFollower(actor)
    ctrl = f.run_step()
    assert ctrl.brake == 1.0
    assert ctrl.throttle == 0.0
