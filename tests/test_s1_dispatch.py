"""S1FireScenario — 8-command dispatch (R4-02 + R4-05)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from carlabridge.commands.bus import CommandBus
from carlabridge.commands.enum import CommandKind, ParsedCommand, RejectCommand
from carlabridge.core.fleet import CarlaActorMember, Fleet, Pose, VirtualMember
from carlabridge.core.incident import Incident
from carlabridge.obs.event_log import EventLog
from carlabridge.scenarios.s1_fire import (
    EXTINGUISH_DWELL_S,
    EXTINGUISH_RADIUS_M,
    UAV_ARRIVAL_EPS_M,
    S1FireScenario,
)
from carlabridge.sensors.camera import CameraManager
from tests.test_camera_manager import FakeSpawner


# ---- fakes ----------------------------------------------------------------


@dataclass
class _FakeUgvActor:
    id: int = 1
    type_id: str = "vehicle.lincoln.mkz_2020"
    applied_controls: list = field(default_factory=list)
    destroyed: bool = False

    def get_transform(self):
        from tests.fakes.fake_world import FakeLocation, FakeRotation, FakeTransform
        return FakeTransform(FakeLocation(0, 0, 0), FakeRotation(0, 0, 0))

    def get_velocity(self):
        from tests.fakes.fake_world import FakeVelocity
        return FakeVelocity()

    def apply_control(self, control) -> None:
        self.applied_controls.append(control)

    def destroy(self) -> None:
        self.destroyed = True


class _FakeFollower:
    def __init__(self, actor) -> None:
        self.actor = actor
        self.destination: tuple[float, float, float] | None = None
        self._steps = 0
        self._done_after = 999
        self.target_speed_mps: float | None = None

    def set_destination(self, world, location) -> None:  # noqa: ARG002
        self.destination = (location.x, location.y, location.z)

    def run_step(self):
        self._steps += 1
        return ("ctrl", self._steps)

    def done(self) -> bool:
        return self._steps >= self._done_after


@dataclass
class _FakeWorldFacade:
    carla_world: Any = None


class _HarnessS1Scenario(S1FireScenario):
    """Bypass real CARLA spawn via direct fleet setup; inject a fake follower."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fake_followers: list[_FakeFollower] = []

    def _make_follower(self, ugv_actor, target_speed_mps):
        f = _FakeFollower(ugv_actor)
        f.target_speed_mps = target_speed_mps
        self.fake_followers.append(f)
        return f

    def _set_destination(self, follower, dest_xyz):
        class _Loc:
            x, y, z = dest_xyz

        follower.set_destination(None, _Loc())


def _make_scenario() -> tuple[_HarnessS1Scenario, Fleet, _FakeUgvActor, CommandBus, list[dict]]:
    fleet = Fleet()
    ugv_actor = _FakeUgvActor(id=42)
    fleet.register(CarlaActorMember(entity_id="UGV-01", role="dispatchable", actor=ugv_actor))
    fleet.set_origin("UGV-01", Pose(x=0, y=0, z=0))
    for i, dx in enumerate((-20.0, 0.0, 20.0), start=1):
        eid = f"UAV-0{i}"
        origin = Pose(x=dx, y=0, z=60, yaw=0)
        uav = VirtualMember(
            entity_id=eid, role="patrol", _pose=origin,
            altitude=60, heading=0, battery=100,
        )
        fleet.register(uav)
        fleet.set_origin(eid, origin)
    bus = CommandBus(maxsize=8)
    statuses: list[dict] = []
    bus.set_on_command_status(statuses.append)
    evlog = EventLog(capacity=200)
    scen = _HarnessS1Scenario(
        world=_FakeWorldFacade(),
        fleet=fleet,
        camera_manager=CameraManager(spawner=FakeSpawner()),
        event_log=evlog,
        command_bus=bus,
    )
    scen.attach_sim_time_provider(lambda: 0.0)
    return scen, fleet, ugv_actor, bus, statuses


def _cmd(idx: int, kind: CommandKind, target: str, params: dict | None = None) -> ParsedCommand:
    return ParsedCommand(id=f"cmd-{idx:02d}", kind=kind, target=target, params=params or {})


# ---- UAV_PATROL ----------------------------------------------------------


def test_uav_patrol_loop_false_walks_to_finish_then_completes():
    scen, fleet, _, _, statuses = _make_scenario()
    uav = fleet.get("UAV-01")
    assert isinstance(uav, VirtualMember)

    path = [{"x": 0, "y": 0, "z": 60}, {"x": 5, "y": 0, "z": 60}]
    scen.on_command(_cmd(1, CommandKind.UAV_PATROL, "UAV-01",
                         {"path": path, "cruise_speed": 100.0, "loop": False}))
    # Tick a few times: should walk both waypoints, then complete.
    for _ in range(50):
        scen.on_tick_post(sim_time=1.0)
        if not scen._in_flight:
            break
    assert scen._in_flight == {}
    completed = [s for s in statuses if s["status"] == "completed"]
    assert len(completed) == 1
    assert completed[0]["cmd_id"] == "cmd-01"


def test_uav_patrol_loop_true_emits_ongoing_and_never_auto_completes():
    scen, _, _, _, statuses = _make_scenario()
    path = [{"x": 0, "y": 0, "z": 60}, {"x": 5, "y": 0, "z": 60}]
    scen.on_command(_cmd(1, CommandKind.UAV_PATROL, "UAV-01",
                         {"path": path, "cruise_speed": 100.0, "loop": True}))

    ongoing = [s for s in statuses if s["status"] == "ongoing"]
    assert len(ongoing) == 1 and ongoing[0]["cmd_id"] == "cmd-01"

    # Even after extensive ticking, status stays in-flight.
    for _ in range(60):
        scen.on_tick_post(sim_time=1.0)
    assert "cmd-01" in scen._in_flight
    completions = [s for s in statuses if s["status"] == "completed"]
    assert completions == []


def test_uav_patrol_rejects_when_target_is_ugv():
    scen, _, _, _, _ = _make_scenario()
    with pytest.raises(RejectCommand) as ei:
        scen.on_command(_cmd(1, CommandKind.UAV_PATROL, "UGV-01",
                             {"path": [{"x": 0, "y": 0, "z": 60}],
                              "cruise_speed": 5.0}))
    assert ei.value.reason == "unknown_target"


# ---- UAV_GOTO -----------------------------------------------------------


def test_uav_goto_arrives_within_eps_completes():
    scen, fleet, _, _, statuses = _make_scenario()
    uav = fleet.get("UAV-02")
    assert isinstance(uav, VirtualMember)
    uav.set_pose(Pose(x=0.4, y=0.0, z=60.0))

    scen.on_command(_cmd(1, CommandKind.UAV_GOTO, "UAV-02",
                         {"waypoint": {"x": 0.0, "y": 0.0, "z": 60.0},
                          "cruise_speed": 1.0}))
    # Already inside eps (< 0.5) — one tick (which still does a step) will
    # snap and complete.
    scen.on_tick_post(sim_time=0.1)
    assert scen._in_flight == {}
    assert [s["status"] for s in statuses] == ["completed"]


def test_uav_goto_far_target_stays_in_flight():
    scen, fleet, _, _, _ = _make_scenario()
    uav = fleet.get("UAV-02")
    assert isinstance(uav, VirtualMember)
    uav.set_pose(Pose(x=0, y=0, z=60))

    scen.on_command(_cmd(1, CommandKind.UAV_GOTO, "UAV-02",
                         {"waypoint": {"x": 1000.0, "y": 0.0, "z": 60.0},
                          "cruise_speed": 0.1}))
    scen.on_tick_post(sim_time=0.1)
    assert "cmd-01" in scen._in_flight


# ---- UAV_RTL ------------------------------------------------------------


def test_uav_rtl_uses_fleet_origin():
    scen, fleet, _, _, statuses = _make_scenario()
    uav = fleet.get("UAV-02")
    assert isinstance(uav, VirtualMember)
    uav.set_pose(Pose(x=500.0, y=500.0, z=60.0))

    scen.on_command(_cmd(1, CommandKind.UAV_RTL, "UAV-02"))
    # Origin for UAV-02 was set to (0, 0, 60) in _make_scenario.
    assert "cmd-01" in scen._in_flight
    in_flt = scen._in_flight["cmd-01"]
    assert in_flt.state["target"] == fleet.get_origin("UAV-02")


def test_uav_rtl_no_origin_rejects():
    scen, fleet, _, _, _ = _make_scenario()
    fleet.set_origin  # noqa: B018
    fleet._origins.pop("UAV-03")
    with pytest.raises(RejectCommand) as ei:
        scen.on_command(_cmd(1, CommandKind.UAV_RTL, "UAV-03"))
    assert ei.value.reason == "no_origin"


# ---- UAV_HOLD -----------------------------------------------------------


def test_uav_hold_clears_target_and_completes_same_tick():
    scen, fleet, _, _, statuses = _make_scenario()
    uav = fleet.get("UAV-01")
    assert isinstance(uav, VirtualMember)
    uav.set_target(Pose(x=999, y=999, z=60), cruise_speed=10.0)

    scen.on_command(_cmd(1, CommandKind.UAV_HOLD, "UAV-01"))
    assert uav.target is None  # target cleared at accept

    scen.on_tick_post(sim_time=0.1)
    assert scen._in_flight == {}
    assert any(s["status"] == "completed" and s["cmd_id"] == "cmd-01" for s in statuses)


# ---- UGV_GOTO -----------------------------------------------------------


def test_ugv_goto_creates_follower_with_destination():
    scen, _, _, _, _ = _make_scenario()
    scen.on_command(_cmd(1, CommandKind.UGV_GOTO, "UGV-01",
                         {"dest": {"x": 100.0, "y": 50.0, "z": 0.0}}))
    assert scen._ugv_follower is not None
    assert scen.fake_followers[0].destination == (100.0, 50.0, 0.0)


def test_ugv_goto_target_speed_converted_to_mps():
    scen, _, _, _, _ = _make_scenario()
    scen.on_command(_cmd(1, CommandKind.UGV_GOTO, "UGV-01",
                         {"dest": {"x": 50.0, "y": 0.0, "z": 0.0},
                          "target_speed": 36.0}))  # 36 km/h = 10 m/s
    assert scen.fake_followers[0].target_speed_mps == pytest.approx(10.0)


def test_ugv_goto_unknown_target_rejects_with_canonical_reason():
    scen, _, _, _, _ = _make_scenario()
    with pytest.raises(RejectCommand) as ei:
        scen.on_command(_cmd(1, CommandKind.UGV_GOTO, "UGV-99",
                             {"dest": {"x": 1.0, "y": 2.0, "z": 0.0}}))
    assert ei.value.reason == "unknown_target"


def test_ugv_goto_arrival_via_follower_done_completes():
    scen, _, ugv_actor, _, statuses = _make_scenario()
    scen.on_command(_cmd(1, CommandKind.UGV_GOTO, "UGV-01",
                         {"dest": {"x": 50.0, "y": 0.0, "z": 0.0}}))
    scen.fake_followers[0]._done_after = 1
    scen.on_tick_post(sim_time=0.1)
    assert scen._in_flight == {}
    assert any(s["status"] == "completed" and s["cmd_id"] == "cmd-01" for s in statuses)
    assert len(ugv_actor.applied_controls) == 1


# ---- UGV_RTL ------------------------------------------------------------


def test_ugv_rtl_targets_origin():
    scen, _, _, _, _ = _make_scenario()
    scen.on_command(_cmd(1, CommandKind.UGV_RTL, "UGV-01"))
    assert scen.fake_followers[0].destination == (0.0, 0.0, 0.0)


# ---- UGV_EXTINGUISH -----------------------------------------------------


def test_ugv_extinguish_within_range_completes_after_dwell_and_destroys_actor():
    scen, fleet, _, _, statuses = _make_scenario()
    actor = _FakeUgvActor(id=99)
    scen._fire_actors["fire-001"] = actor
    fleet.add_incident(Incident(
        id="fire-001", kind="fire", position=Pose(x=2, y=0, z=0),
        severity="high", since_sim_time=0,
    ))

    scen.on_command(_cmd(1, CommandKind.UGV_EXTINGUISH, "UGV-01",
                         {"incident_id": "fire-001"}))
    scen.on_tick_post(sim_time=0.1)
    assert "cmd-01" in scen._in_flight
    assert not actor.destroyed
    assert fleet.get_incident("fire-001") is not None

    scen.on_tick_post(sim_time=EXTINGUISH_DWELL_S + 0.05)
    assert scen._in_flight == {}
    assert actor.destroyed
    assert fleet.get_incident("fire-001") is None
    assert any(s["status"] == "completed" and s["cmd_id"] == "cmd-01" for s in statuses)


def test_ugv_extinguish_out_of_range_rejected_with_distance_detail():
    scen, fleet, _, _, _ = _make_scenario()
    fleet.add_incident(Incident(
        id="fire-001", kind="fire", position=Pose(x=100, y=0, z=0),
        severity="high", since_sim_time=0,
    ))
    with pytest.raises(RejectCommand) as ei:
        scen.on_command(_cmd(1, CommandKind.UGV_EXTINGUISH, "UGV-01",
                             {"incident_id": "fire-001"}))
    assert ei.value.reason == "not_in_range"
    assert ei.value.detail["max_m"] == EXTINGUISH_RADIUS_M
    assert ei.value.detail["distance_m"] >= 99.0


def test_ugv_extinguish_unknown_incident_rejected():
    scen, _, _, _, _ = _make_scenario()
    with pytest.raises(RejectCommand) as ei:
        scen.on_command(_cmd(1, CommandKind.UGV_EXTINGUISH, "UGV-01",
                             {"incident_id": "fire-nope"}))
    assert ei.value.reason == "unknown_incident"


# ---- UGV_STOP -----------------------------------------------------------


def test_ugv_stop_clears_follower_and_completes_same_tick():
    scen, _, ugv_actor, _, statuses = _make_scenario()
    scen.on_command(_cmd(1, CommandKind.UGV_GOTO, "UGV-01",
                         {"dest": {"x": 50.0, "y": 0.0, "z": 0.0}}))
    assert scen._ugv_follower is not None

    scen.on_command(_cmd(2, CommandKind.UGV_STOP, "UGV-01"))
    assert scen._ugv_follower is None
    # apply_control was called with brake
    assert ugv_actor.applied_controls  # at least one brake
    last = ugv_actor.applied_controls[-1]
    assert getattr(last, "brake", 0) == 1.0

    scen.on_tick_post(sim_time=0.1)
    # cmd-01 cancelled with explicit_stop; cmd-02 completed instant.
    seq = [(s["cmd_id"], s["status"], s.get("reason")) for s in statuses]
    assert ("cmd-01", "cancelled", "explicit_stop") in seq
    assert ("cmd-02", "completed", None) in seq


# ---- scenario_resetting guard -------------------------------------------


def test_on_command_rejected_while_resetting():
    scen, _, _, _, _ = _make_scenario()
    scen._resetting = True
    with pytest.raises(RejectCommand) as ei:
        scen.on_command(_cmd(1, CommandKind.UAV_HOLD, "UAV-01"))
    assert ei.value.reason == "scenario_resetting"
