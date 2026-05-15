"""End-to-end: UAV_PATROL loop=true ongoing + supersede cancelled (R5-04)."""

from __future__ import annotations

import asyncio

import pytest

from carlabridge.commands.bus import CommandBus
from carlabridge.commands.enum import CommandKind, ParsedCommand
from carlabridge.core.fleet import Fleet, Pose, VirtualMember
from carlabridge.obs.event_log import EventLog
from carlabridge.scenarios.s1_fire import S1FireScenario
from carlabridge.sensors.camera import CameraManager
from tests.test_camera_manager import FakeSpawner


# ---- harness reusing the dispatch-test fixture pattern -------------------


class _FakeWorld:
    carla_world = None


def _make_scenario() -> tuple[S1FireScenario, list[dict]]:
    fleet = Fleet()
    for i, dx in enumerate((-20.0, 0.0, 20.0), start=1):
        eid = f"UAV-0{i}"
        origin = Pose(x=dx, y=0, z=60, yaw=0)
        fleet.register(VirtualMember(
            entity_id=eid, role="patrol", _pose=origin,
            altitude=60, heading=0, battery=100,
        ))
        fleet.set_origin(eid, origin)
    bus = CommandBus(maxsize=8)
    statuses: list[dict] = []
    bus.set_on_command_status(statuses.append)
    scen = S1FireScenario(
        world=_FakeWorld(),
        fleet=fleet,
        camera_manager=CameraManager(spawner=FakeSpawner()),
        event_log=EventLog(capacity=200),
        command_bus=bus,
    )
    scen.attach_sim_time_provider(lambda: 0.0)
    return scen, statuses


def _patrol(idx: int, target: str, loop: bool) -> ParsedCommand:
    return ParsedCommand(
        id=f"cmd-{idx:02d}", kind=CommandKind.UAV_PATROL, target=target,
        params={
            "path": [{"x": 0, "y": 0, "z": 60}, {"x": 5, "y": 0, "z": 60}],
            "cruise_speed": 5.0,
            "loop": loop,
        },
    )


# ---- ongoing on accept ---------------------------------------------------


def test_patrol_loop_true_emits_ongoing_immediately_on_accept():
    scen, statuses = _make_scenario()
    scen.on_command(_patrol(1, "UAV-01", loop=True))
    ongoings = [s for s in statuses if s["status"] == "ongoing"]
    assert len(ongoings) == 1
    p = ongoings[0]
    assert p["cmd_id"] == "cmd-01"
    assert p["kind"] == "UAV_PATROL"
    assert p["target"] == "UAV-01"
    assert p["reason"] is None
    assert p["at_sim_time"] == 0.0


def test_patrol_loop_false_does_not_emit_ongoing():
    scen, statuses = _make_scenario()
    scen.on_command(_patrol(1, "UAV-01", loop=False))
    ongoings = [s for s in statuses if s["status"] == "ongoing"]
    assert ongoings == []


def test_patrol_loop_true_stays_in_flight_even_after_many_ticks():
    scen, statuses = _make_scenario()
    scen.on_command(_patrol(1, "UAV-01", loop=True))
    for _ in range(500):
        scen.on_tick_post(sim_time=1.0)
    assert "cmd-01" in scen._in_flight
    completions = [s for s in statuses if s["status"] == "completed"]
    assert completions == []


# ---- supersede cancels ongoing ------------------------------------------


def test_patrol_loop_true_cancelled_when_superseded_by_new_command():
    scen, statuses = _make_scenario()
    scen.on_command(_patrol(1, "UAV-01", loop=True))
    # Supersede with HOLD (instant).
    scen.on_command(ParsedCommand(id="cmd-02", kind=CommandKind.UAV_HOLD, target="UAV-01"))
    cancellations = [s for s in statuses if s["status"] == "cancelled"]
    assert len(cancellations) == 1
    c = cancellations[0]
    assert c["cmd_id"] == "cmd-01"
    assert c["reason"] == "superseded"
    assert c["detail"] == {"by_cmd_id": "cmd-02"}


def test_patrol_loop_true_cancelled_on_reset():
    """Reset must cancel ongoing PATROL with reason=reset."""
    scen, statuses = _make_scenario()
    scen.on_command(_patrol(1, "UAV-01", loop=True))
    cancelled = scen._cancel_all_in_flight(reason="reset", sim_time=2.0)
    assert cancelled == ["cmd-01"]
    cancellations = [s for s in statuses if s["status"] == "cancelled"]
    assert cancellations[-1]["cmd_id"] == "cmd-01"
    assert cancellations[-1]["reason"] == "reset"


def test_patrol_loop_true_supersede_by_new_patrol_loop_true():
    """Replacing one ongoing PATROL with another: first cancelled(superseded),
    second emits its own ongoing."""
    scen, statuses = _make_scenario()
    scen.on_command(_patrol(1, "UAV-01", loop=True))
    scen.on_command(_patrol(2, "UAV-01", loop=True))
    seq = [(s["cmd_id"], s["status"]) for s in statuses]
    assert seq[0] == ("cmd-01", "ongoing")
    assert ("cmd-01", "cancelled") in seq
    assert ("cmd-02", "ongoing") in seq


def test_patrol_loop_false_natural_completion_emits_completed():
    scen, statuses = _make_scenario()
    scen.on_command(_patrol(1, "UAV-01", loop=False))
    # Tick until the UAV walks the full path.
    for _ in range(200):
        scen.on_tick_post(sim_time=1.0)
        if not scen._in_flight:
            break
    assert scen._in_flight == {}
    completions = [s for s in statuses if s["status"] == "completed"]
    assert len(completions) == 1
    assert completions[0]["cmd_id"] == "cmd-01"
