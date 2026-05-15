"""Scenario base — _accept_command / _check_completion / _finalize_command (R3)."""

from __future__ import annotations

import pytest

from carlabridge.commands.bus import CommandBus
from carlabridge.commands.enum import CommandKind, ParsedCommand
from carlabridge.core.fleet import Fleet
from carlabridge.obs.event_log import EventLog
from carlabridge.scenarios.base import CompletionResult, Scenario
from carlabridge.scenarios.in_flight import InFlightCommand


class _FakeWorld:
    """Minimal stand-in: Scenario base only stashes the object."""


class _FakeCameraManager:
    """Minimal stand-in: Scenario base only stashes the object."""


def _make_scenario(*, with_bus: bool = True) -> tuple[Scenario, EventLog, CommandBus | None]:
    evlog = EventLog(capacity=200)
    bus: CommandBus | None = None
    if with_bus:
        bus = CommandBus(maxsize=4)
    scen = Scenario(
        world=_FakeWorld(),
        fleet=Fleet(),
        camera_manager=_FakeCameraManager(),
        event_log=evlog,
        command_bus=bus,
    )
    return scen, evlog, bus


def _cmd(idx: int, kind: CommandKind, target: str) -> ParsedCommand:
    return ParsedCommand(id=f"cmd-{idx:02d}", kind=kind, target=target)


# ---- accept registers in-flight indexes ----------------------------------


def test_accept_command_registers_in_flight():
    scen, _, _ = _make_scenario(with_bus=False)
    scen._accept_command(_cmd(1, CommandKind.UAV_HOLD, "UAV-01"), 1.0, "instant")
    assert "cmd-01" in scen._in_flight
    assert scen._in_flight_by_entity["UAV-01"] == "cmd-01"


def test_accept_command_records_accepted_at_sim_time():
    scen, _, _ = _make_scenario(with_bus=False)
    scen._accept_command(_cmd(1, CommandKind.UAV_GOTO, "UAV-01"), 12.34, "uav_arrival")
    assert scen._in_flight["cmd-01"].accepted_at_sim_time == 12.34


def test_in_flight_snapshot_wire_shape_and_sort():
    scen, _, _ = _make_scenario(with_bus=False)
    scen._accept_command(_cmd(2, CommandKind.UGV_GOTO, "UGV-01"), 2.0, "ugv_arrival")
    scen._accept_command(_cmd(1, CommandKind.UAV_PATROL, "UAV-01"), 1.0, "ongoing")
    snap = scen.in_flight_snapshot()
    assert [e["cmd_id"] for e in snap] == ["cmd-01", "cmd-02"]
    assert snap[0] == {
        "cmd_id": "cmd-01",
        "kind": "UAV_PATROL",
        "target": "UAV-01",
        "accepted_at_sim_time": 1.0,
    }


# ---- default _check_completion -------------------------------------------


def test_default_check_completion_instant_completes_immediately():
    scen, _, _ = _make_scenario(with_bus=False)
    scen._accept_command(_cmd(1, CommandKind.UAV_HOLD, "UAV-01"), 1.0, "instant")
    in_flt = scen._in_flight["cmd-01"]
    result = scen._check_completion(in_flt, sim_time=1.0)
    assert result is not None
    assert result.status == "completed"
    assert result.reason is None


def test_default_check_completion_ongoing_returns_none():
    scen, _, _ = _make_scenario(with_bus=False)
    scen._accept_command(_cmd(1, CommandKind.UAV_PATROL, "UAV-01"), 1.0, "ongoing")
    in_flt = scen._in_flight["cmd-01"]
    assert scen._check_completion(in_flt, sim_time=1.0) is None


def test_default_check_completion_arrival_returns_none():
    """Base doesn't know GOTO arrival semantics — subclass (R4) extends."""
    scen, _, _ = _make_scenario(with_bus=False)
    scen._accept_command(_cmd(1, CommandKind.UAV_GOTO, "UAV-01"), 1.0, "uav_arrival")
    in_flt = scen._in_flight["cmd-01"]
    assert scen._check_completion(in_flt, sim_time=1.0) is None


# ---- _drive_command_lifecycle drives instant commands --------------------


def test_drive_command_lifecycle_completes_instant_same_tick():
    scen, evlog, bus = _make_scenario()
    seen: list[dict] = []
    assert bus is not None
    bus.set_on_command_status(seen.append)

    scen._accept_command(_cmd(1, CommandKind.UAV_HOLD, "UAV-01"), 5.0, "instant")
    assert "cmd-01" in scen._in_flight

    # Same tick → drive lifecycle → instant must complete.
    scen._drive_command_lifecycle(sim_time=5.0)

    assert "cmd-01" not in scen._in_flight
    assert "UAV-01" not in scen._in_flight_by_entity
    assert len(seen) == 1
    p = seen[0]
    assert p["cmd_id"] == "cmd-01"
    assert p["status"] == "completed"
    assert p["kind"] == "UAV_HOLD"
    assert p["target"] == "UAV-01"
    assert p["reason"] is None
    assert p["at_sim_time"] == 5.0

    # event_log carries the cmd_id correlation per design §3.5.
    events = evlog.recent()
    correlated = [e for e in events if e.cmd_id == "cmd-01"]
    assert correlated, "expected an event_log entry with cmd_id=cmd-01"
    assert correlated[-1].severity == "ok"


def test_drive_command_lifecycle_leaves_ongoing_in_flight():
    scen, _, bus = _make_scenario()
    seen: list[dict] = []
    assert bus is not None
    bus.set_on_command_status(seen.append)

    scen._accept_command(_cmd(1, CommandKind.UAV_PATROL, "UAV-01"), 1.0, "ongoing")
    scen._drive_command_lifecycle(sim_time=2.0)

    assert "cmd-01" in scen._in_flight
    assert seen == []


def test_drive_command_lifecycle_is_safe_on_empty_in_flight():
    scen, _, _ = _make_scenario()
    scen._drive_command_lifecycle(sim_time=1.0)  # must not raise


# ---- _finalize_command broadcast shape ------------------------------------


def test_finalize_command_failed_includes_reason_and_detail():
    scen, evlog, bus = _make_scenario()
    seen: list[dict] = []
    assert bus is not None
    bus.set_on_command_status(seen.append)

    scen._accept_command(_cmd(1, CommandKind.UGV_GOTO, "UGV-01"), 1.0, "ugv_arrival")
    in_flt = scen._in_flight["cmd-01"]
    scen._finalize_command(
        "cmd-01", in_flt,
        CompletionResult.failed("follower_error", {"message": "boom"}),
        sim_time=4.5,
    )
    assert "cmd-01" not in scen._in_flight
    assert "UGV-01" not in scen._in_flight_by_entity
    p = seen[0]
    assert p["status"] == "failed"
    assert p["reason"] == "follower_error"
    assert p["detail"] == {"message": "boom"}
    assert p["at_sim_time"] == 4.5
    # Failed → severity danger in event_log.
    danger = [e for e in evlog.recent() if e.cmd_id == "cmd-01" and e.severity == "danger"]
    assert danger


def test_finalize_command_without_bus_still_writes_event_log():
    scen, evlog, _ = _make_scenario(with_bus=False)
    scen._accept_command(_cmd(1, CommandKind.UAV_HOLD, "UAV-01"), 1.0, "instant")
    in_flt = scen._in_flight["cmd-01"]
    # No bus wired — must not raise.
    scen._finalize_command(
        "cmd-01", in_flt, CompletionResult.completed(), sim_time=1.0
    )
    assert "cmd-01" not in scen._in_flight
    correlated = [e for e in evlog.recent() if e.cmd_id == "cmd-01"]
    assert correlated


def test_cancel_all_in_flight_returns_cancelled_ids():
    scen, _, bus = _make_scenario()
    seen: list[dict] = []
    assert bus is not None
    bus.set_on_command_status(seen.append)

    scen._accept_command(_cmd(1, CommandKind.UAV_GOTO, "UAV-01"), 1.0, "uav_arrival")
    scen._accept_command(_cmd(2, CommandKind.UGV_GOTO, "UGV-01"), 1.5, "ugv_arrival")
    cancelled = scen._cancel_all_in_flight(reason="reset", sim_time=3.0)

    assert set(cancelled) == {"cmd-01", "cmd-02"}
    assert scen._in_flight == {}
    assert scen._in_flight_by_entity == {}
    assert all(p["status"] == "cancelled" and p["reason"] == "reset" for p in seen)


def test_completion_result_helpers():
    r = CompletionResult.completed()
    assert (r.status, r.reason, r.detail) == ("completed", None, None)
    r = CompletionResult.failed("entity_destroyed")
    assert r.status == "failed" and r.reason == "entity_destroyed"
    r = CompletionResult.cancelled("reset", {"by": "http"})
    assert r.status == "cancelled" and r.detail == {"by": "http"}


def test_finalize_command_clears_bus_submit_timestamp():
    """When submit recorded a timestamp, finalize must drop it so the bus
    doesn't leak per-cmd bookkeeping."""
    scen, _, bus = _make_scenario()
    assert bus is not None
    cmd = _cmd(1, CommandKind.UAV_HOLD, "UAV-01")
    bus.submit(cmd)
    assert bus.submitted_at(cmd.id) is not None
    scen._accept_command(cmd, 1.0, "instant")
    in_flt = scen._in_flight[cmd.id]
    scen._finalize_command(cmd.id, in_flt, CompletionResult.completed(), 1.0)
    assert bus.submitted_at(cmd.id) is None
