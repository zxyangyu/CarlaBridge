"""Supersede semantics — Scenario._accept_command (R3-03)."""

from __future__ import annotations

from carlabridge.commands.bus import CommandBus
from carlabridge.commands.enum import CommandKind, ParsedCommand
from carlabridge.core.fleet import Fleet
from carlabridge.obs.event_log import EventLog
from carlabridge.scenarios.base import Scenario


class _FakeWorld:  # minimal stash target
    pass


class _FakeCameraManager:
    pass


def _make_scenario() -> tuple[Scenario, CommandBus, list[dict]]:
    evlog = EventLog(capacity=200)
    bus = CommandBus(maxsize=4)
    statuses: list[dict] = []
    bus.set_on_command_status(statuses.append)
    scen = Scenario(
        world=_FakeWorld(),
        fleet=Fleet(),
        camera_manager=_FakeCameraManager(),
        event_log=evlog,
        command_bus=bus,
    )
    return scen, bus, statuses


def _cmd(idx: int, kind: CommandKind, target: str) -> ParsedCommand:
    return ParsedCommand(id=f"cmd-{idx:02d}", kind=kind, target=target)


# ---- two long commands on same entity ------------------------------------


def test_second_command_supersedes_first_with_reason_superseded():
    scen, _, statuses = _make_scenario()

    scen._accept_command(_cmd(1, CommandKind.UAV_GOTO, "UAV-01"), 1.0, "uav_arrival")
    scen._accept_command(_cmd(2, CommandKind.UAV_RTL, "UAV-01"), 2.0, "uav_arrival")

    # Only one command must remain in-flight for the entity.
    assert "cmd-01" not in scen._in_flight
    assert "cmd-02" in scen._in_flight
    assert scen._in_flight_by_entity["UAV-01"] == "cmd-02"

    # One status emitted: the cancellation of the old cmd.
    cancellations = [s for s in statuses if s["status"] == "cancelled"]
    assert len(cancellations) == 1
    c = cancellations[0]
    assert c["cmd_id"] == "cmd-01"
    assert c["reason"] == "superseded"
    assert c["detail"] == {"by_cmd_id": "cmd-02"}
    assert c["at_sim_time"] == 2.0


def test_supersede_only_affects_same_entity():
    scen, _, statuses = _make_scenario()
    scen._accept_command(_cmd(1, CommandKind.UAV_GOTO, "UAV-01"), 1.0, "uav_arrival")
    scen._accept_command(_cmd(2, CommandKind.UGV_GOTO, "UGV-01"), 1.5, "ugv_arrival")
    # No supersede across entities.
    assert "cmd-01" in scen._in_flight
    assert "cmd-02" in scen._in_flight
    assert statuses == []


def test_supersede_clears_old_entity_index():
    scen, _, _ = _make_scenario()
    scen._accept_command(_cmd(1, CommandKind.UAV_GOTO, "UAV-01"), 1.0, "uav_arrival")
    scen._accept_command(_cmd(2, CommandKind.UAV_GOTO, "UAV-01"), 2.0, "uav_arrival")
    # The entity index points to the new cmd, not the old.
    assert scen._in_flight_by_entity["UAV-01"] == "cmd-02"


# ---- UGV_STOP carries explicit_stop reason on superseded ----------------


def test_ugv_stop_supersedes_with_explicit_stop_reason():
    scen, _, statuses = _make_scenario()
    scen._accept_command(_cmd(1, CommandKind.UGV_GOTO, "UGV-01"), 1.0, "ugv_arrival")
    scen._accept_command(_cmd(2, CommandKind.UGV_STOP, "UGV-01"), 2.0, "instant")

    cancellations = [s for s in statuses if s["status"] == "cancelled"]
    assert len(cancellations) == 1
    c = cancellations[0]
    assert c["cmd_id"] == "cmd-01"
    assert c["reason"] == "explicit_stop"
    assert c["detail"] == {"by_cmd_id": "cmd-02"}


def test_ugv_stop_completes_in_same_tick_after_supersede():
    """Per design §6.4 example: STOP supersedes prior, then itself completes
    same tick. Two statuses fly: cmd-01 cancelled + cmd-02 completed."""
    scen, _, statuses = _make_scenario()
    scen._accept_command(_cmd(1, CommandKind.UGV_GOTO, "UGV-01"), 1.0, "ugv_arrival")
    scen._accept_command(_cmd(2, CommandKind.UGV_STOP, "UGV-01"), 2.0, "instant")
    scen._drive_command_lifecycle(sim_time=2.0)

    assert scen._in_flight == {}
    assert scen._in_flight_by_entity == {}
    seq = [(s["cmd_id"], s["status"], s.get("reason")) for s in statuses]
    assert seq == [
        ("cmd-01", "cancelled", "explicit_stop"),
        ("cmd-02", "completed", None),
    ]


# ---- supersede chain ------------------------------------------------------


def test_supersede_chain_emits_one_cancel_per_step():
    scen, _, statuses = _make_scenario()
    scen._accept_command(_cmd(1, CommandKind.UAV_GOTO, "UAV-01"), 1.0, "uav_arrival")
    scen._accept_command(_cmd(2, CommandKind.UAV_HOLD, "UAV-01"), 1.5, "instant")
    scen._accept_command(_cmd(3, CommandKind.UAV_RTL, "UAV-01"), 2.0, "uav_arrival")

    cancellations = [s for s in statuses if s["status"] == "cancelled"]
    assert [c["cmd_id"] for c in cancellations] == ["cmd-01", "cmd-02"]
    assert all(c["reason"] == "superseded" for c in cancellations)
    assert scen._in_flight_by_entity["UAV-01"] == "cmd-03"


def test_supersede_does_not_emit_status_for_new_cmd():
    """Accept itself is silent — only the *old* cmd's cancel is emitted.
    The new cmd's accept lives in the sio.call return value (R5)."""
    scen, _, statuses = _make_scenario()
    scen._accept_command(_cmd(1, CommandKind.UAV_GOTO, "UAV-01"), 1.0, "uav_arrival")
    scen._accept_command(_cmd(2, CommandKind.UAV_RTL, "UAV-01"), 2.0, "uav_arrival")
    new_cmd_statuses = [s for s in statuses if s["cmd_id"] == "cmd-02"]
    assert new_cmd_statuses == []


def test_supersede_drops_only_old_bus_timestamp():
    scen, bus, _ = _make_scenario()
    a = _cmd(1, CommandKind.UAV_GOTO, "UAV-01")
    b = _cmd(2, CommandKind.UAV_RTL, "UAV-01")
    bus.submit(a)
    bus.submit(b)
    scen._accept_command(a, 1.0, "uav_arrival")
    scen._accept_command(b, 2.0, "uav_arrival")
    # Old cancelled → its timestamp is forgotten; new still tracked.
    assert bus.submitted_at("cmd-01") is None
    assert bus.submitted_at("cmd-02") is not None
