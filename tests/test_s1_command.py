"""S1 scenario on_command + mock_agent_loop tests.

We patch BasicAgent + the world transform helper so the scenario runs without
CARLA. The scenario reaches into `agents.navigation.basic_agent.BasicAgent`
inside `_make_basic_agent`; we override that on the instance.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from carlabridge.commands.enum import CommandKind, ParsedCommand, RejectCommand
from carlabridge.core.fleet import CarlaActorMember, Fleet, VirtualMember
from carlabridge.obs.event_log import EventLog
from carlabridge.scenarios.base import Scenario
from carlabridge.scenarios.s1_fire import _SCRIPT, S1FireScenario
from carlabridge.sensors.camera import CameraManager
from tests.test_camera_manager import FakeSpawner


# ---------- fakes for CARLA + BasicAgent ----------------------------------


@dataclass
class FakeUgvActor:
    """A throwaway UGV-like CARLA actor for unit tests."""

    id: int = 1
    type_id: str = "vehicle.lincoln.mkz_2020"
    applied_controls: list = field(default_factory=list)
    destroyed: bool = False

    def get_transform(self):  # only consumed by Fleet.pose() in some paths
        from tests.fakes.fake_world import FakeLocation, FakeRotation, FakeTransform

        return FakeTransform(FakeLocation(0, 0, 0), FakeRotation(0, 0, 0))

    def get_velocity(self):
        from tests.fakes.fake_world import FakeVelocity

        return FakeVelocity()

    def apply_control(self, control) -> None:
        self.applied_controls.append(control)

    def destroy(self) -> None:
        self.destroyed = True


class FakeBasicAgent:
    """Stand-in for agents.navigation.basic_agent.BasicAgent."""

    def __init__(self, actor, target_speed: float = 25.0) -> None:
        self.actor = actor
        self.target_speed = target_speed
        self.destination: Any | None = None
        self._steps = 0
        self._done_after = 999

    def set_destination(self, location) -> None:
        self.destination = (location.x, location.y, location.z)

    def run_step(self):
        self._steps += 1
        # Return a sentinel "control" object — the actor just records it.
        return ("ctrl", self._steps)

    def done(self) -> bool:
        return self._steps >= self._done_after


@dataclass
class FakeWorldFacade:
    carla_world: Any


class _HarnessS1Scenario(S1FireScenario):
    """Wires fake BasicAgent + bypasses CARLA spawn so unit tests can run.

    Setup is bypassed by the caller (we directly populate the fleet and origin
    state). on_command + on_tick_post are exercised against this skeleton.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fake_agents: list[FakeBasicAgent] = []

    def _make_basic_agent(self, ugv_actor):
        a = FakeBasicAgent(ugv_actor)
        self.fake_agents.append(a)
        return a

    def _set_destination(self, agent, dest_xyz):
        # Bypass carla.Location wrapping — pass a tuple-like.
        class _Loc:
            x, y, z = dest_xyz

        agent.set_destination(_Loc())


# ---------- helpers -------------------------------------------------------


def _make_scenario_with_state() -> tuple[_HarnessS1Scenario, Fleet, FakeUgvActor]:
    fleet = Fleet()
    actor = FakeUgvActor(id=42)
    fleet.register(CarlaActorMember(entity_id="UGV-01", role="dispatchable", actor=actor))
    # Three virtual UAVs at known origins.
    from carlabridge.core.fleet import Pose

    for i, dx in enumerate((-20.0, 0.0, 20.0), start=1):
        eid = f"UAV-0{i}"
        origin = Pose(x=dx, y=0, z=60, yaw=0)
        uav = VirtualMember(
            entity_id=eid, role="patrol",
            _pose=origin, altitude=60, heading=0, battery=100,
        )
        fleet.register(uav)
    scen = _HarnessS1Scenario(
        world=FakeWorldFacade(carla_world=object()),
        fleet=fleet,
        camera_manager=CameraManager(spawner=FakeSpawner()),
        event_log=EventLog(capacity=200),
    )
    # Populate the state setup() would have filled.
    from carlabridge.core.fleet import Pose

    scen._anchor_world_xyz = (0.0, 0.0, 0.0)
    scen._anchor_yaw = 0.0
    scen._ugv_origin = Pose(x=0, y=0, z=0, yaw=0)
    scen._uav_origins = {
        "UAV-01": Pose(x=-20, y=0, z=60, yaw=0),
        "UAV-02": Pose(x=0, y=0, z=60, yaw=0),
        "UAV-03": Pose(x=20, y=0, z=60, yaw=0),
    }
    return scen, fleet, actor


# ---------- UAV commands --------------------------------------------------


def test_uav_rtl_sets_target_to_origin():
    scen, fleet, _ = _make_scenario_with_state()
    uav = fleet.get("UAV-02")
    assert isinstance(uav, VirtualMember)
    # Move UAV away from origin.
    from carlabridge.core.fleet import Pose

    uav.set_pose(Pose(x=200, y=300, z=80))
    cmd = ParsedCommand(id="c1", kind=CommandKind.UAV_RTL, target="UAV-02")
    scen.on_command(cmd)
    assert uav.target is not None
    assert uav.target.x == 0  # origin for UAV-02


def test_uav_hold_clears_target():
    scen, fleet, _ = _make_scenario_with_state()
    uav = fleet.get("UAV-02")
    from carlabridge.core.fleet import Pose
    uav.set_target(Pose(x=999, y=999), cruise_speed=10)
    scen.on_command(ParsedCommand(id="c2", kind=CommandKind.UAV_HOLD, target="UAV-02"))
    assert uav.target is None


def test_uav_rtl_unknown_target_rejects():
    scen, _, _ = _make_scenario_with_state()
    with pytest.raises(RejectCommand):
        scen.on_command(ParsedCommand(id="c3", kind=CommandKind.UAV_RTL, target="UAV-99"))


# ---------- UGV commands --------------------------------------------------


def test_ugv_dispatch_creates_basic_agent_and_runs_step():
    scen, _, actor = _make_scenario_with_state()
    cmd = ParsedCommand(
        id="c4", kind=CommandKind.UGV_DISPATCH, target="UGV-01",
        payload={"x": 100.0, "y": 50.0},
    )
    scen.on_command(cmd)
    assert scen._ugv_basic_agent is not None
    assert scen.fake_agents[0].destination == (100.0, 50.0, 0.0)

    # Tick post drives run_step + apply_control.
    scen.on_tick_post(sim_time=0.1)
    assert len(actor.applied_controls) == 1


def test_ugv_dispatch_with_fire_distance_payload():
    scen, _, _ = _make_scenario_with_state()
    cmd = ParsedCommand(
        id="c5", kind=CommandKind.UGV_DISPATCH, target="UGV-01",
        payload={"fire_distance": 80.0},
    )
    scen.on_command(cmd)
    # anchor x=0 + 80 → destination x=80
    assert scen.fake_agents[0].destination[0] == 80.0


def test_ugv_arrival_clears_basic_agent():
    scen, _, actor = _make_scenario_with_state()
    cmd = ParsedCommand(
        id="c6", kind=CommandKind.UGV_DISPATCH, target="UGV-01",
        payload={"x": 50.0, "y": 0.0},
    )
    scen.on_command(cmd)
    # Force done() True next step.
    scen.fake_agents[0]._done_after = 1
    scen.on_tick_post(sim_time=0.1)
    assert scen._ugv_basic_agent is None  # cleared after done()


def test_ugv_dispatch_unknown_target_rejects():
    scen, _, _ = _make_scenario_with_state()
    with pytest.raises(RejectCommand):
        scen.on_command(ParsedCommand(
            id="c7", kind=CommandKind.UGV_DISPATCH, target="UGV-99",
            payload={"x": 1.0, "y": 2.0},
        ))


def test_ugv_rtl_uses_recorded_origin():
    scen, _, _ = _make_scenario_with_state()
    scen.on_command(ParsedCommand(id="c8", kind=CommandKind.UGV_RTL, target="UGV-01"))
    assert scen.fake_agents[-1].destination == (0.0, 0.0, 0.0)


# ---------- MARK_EVENT / ATTACH_ACTOR --------------------------------


def test_mark_event_writes_event_log():
    scen, _, _ = _make_scenario_with_state()
    scen.on_command(ParsedCommand(
        id="c9", kind=CommandKind.MARK_EVENT, target="",
        payload={"severity": "warn", "message": "fire spreading"},
    ))
    msgs = [e.message for e in scen.event_log.recent()]
    assert "fire spreading" in msgs


def test_attach_actor_is_noop():
    """D3: parsed and acknowledged but no robotic action this milestone."""
    scen, _, _ = _make_scenario_with_state()
    # Should not raise.
    scen.on_command(ParsedCommand(
        id="c10", kind=CommandKind.ATTACH_ACTOR, target="UGV-01",
    ))


# ---------- mock_agent_loop -------------------------------------------


class CaptureLink:
    """Captures emit_command + emit_event_log; on_suggestion not used."""

    def __init__(self):
        self.commands: list[dict] = []
        self.events: list[tuple[str, str, str]] = []

    async def emit_command(self, cmd):
        self.commands.append(cmd)

    async def emit_event_log(self, severity, source, message):
        self.events.append((severity, source, message))

    async def on_suggestion(self, payload):
        pass


async def test_mock_agent_loop_fires_events_at_sim_time(monkeypatch):
    """Drive the SCRIPT with a controlled sim_time so we don't wait 46s."""
    scen, _, _ = _make_scenario_with_state()
    sim_t = [0.0]
    scen.attach_sim_time_provider(lambda: sim_t[0])
    # Shrink the polling interval so the test runs quickly.
    monkeypatch.setattr("carlabridge.scenarios.s1_fire.SCRIPT_TICK_S", 0.005)
    link = CaptureLink()
    task = asyncio.create_task(scen.mock_agent_loop(link))
    # Yield to the event loop so the task captures `_script_start_sim = 0.0`
    # before we start advancing.
    await asyncio.sleep(0.02)
    # Advance sim time past everything in SCRIPT.
    for sim_at in (4.5, 6.5, 7.5, 8.5, 9.5, 25.5, 27.5, 29.5, 45.5, 46.5):
        sim_t[0] = sim_at
        await asyncio.sleep(0.03)
    await asyncio.wait_for(task, timeout=5.0)
    # All cmd events emitted; check a few key ones.
    cmd_targets = [(c["target"], c["text"]) for c in link.commands]
    assert ("UAV-02", "UAV_RTL") in cmd_targets
    assert ("UAV-03", "UAV_RTL") in cmd_targets
    assert ("UGV-01", "UGV_DISPATCH") in cmd_targets
    assert ("UGV-01", "UGV_RTL") in cmd_targets
    assert ("UAV-01", "UAV_HOLD") in cmd_targets
    # Event_log entries fired.
    messages = [m for (_, _, m) in link.events]
    assert any("detected fire" in m for m in messages)
    assert any("scenario complete" in m for m in messages)


async def test_mock_agent_loop_event_count_matches_script(monkeypatch):
    """Run the whole script back-to-back by setting sim_time past everything
    AFTER the task captures its baseline."""
    scen, _, _ = _make_scenario_with_state()
    sim_t = [0.0]
    scen.attach_sim_time_provider(lambda: sim_t[0])
    monkeypatch.setattr("carlabridge.scenarios.s1_fire.SCRIPT_TICK_S", 0.005)
    link = CaptureLink()
    task = asyncio.create_task(scen.mock_agent_loop(link))
    await asyncio.sleep(0.02)  # let task capture _script_start_sim = 0.0
    sim_t[0] = 9999.0  # past everything → script drains immediately
    await asyncio.wait_for(task, timeout=5.0)
    cmd_count = sum(1 for e in _SCRIPT if e.kind == "cmd")
    ev_count = sum(1 for e in _SCRIPT if e.kind == "event_log")
    assert len(link.commands) == cmd_count
    # Only SCRIPT event_log entries reach the link (the "mock agent started"
    # message is written directly to event_log, not via emit_event_log).
    assert len(link.events) == ev_count
