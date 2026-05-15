"""CommandKind / ParsedCommand / RejectCommand basic invariants (R1)."""

from __future__ import annotations

import pytest

from carlabridge.commands.enum import (
    UAV_KINDS,
    UGV_KINDS,
    CommandKind,
    ParsedCommand,
    RejectCommand,
)


# ---- CommandKind ----------------------------------------------------------


def test_command_kind_has_exactly_eight_values():
    assert len(CommandKind) == 8


def test_command_kind_values_match_design_spec():
    names = {k.value for k in CommandKind}
    assert names == {
        "UAV_PATROL", "UAV_GOTO", "UAV_RTL", "UAV_HOLD",
        "UGV_GOTO", "UGV_RTL", "UGV_EXTINGUISH", "UGV_STOP",
    }


def test_command_kind_str_enum_round_trips_through_value():
    for k in CommandKind:
        assert CommandKind(k.value) is k


def test_uav_and_ugv_kind_groups_are_disjoint_and_complete():
    assert UAV_KINDS.isdisjoint(UGV_KINDS)
    assert UAV_KINDS | UGV_KINDS == set(CommandKind)
    assert len(UAV_KINDS) == 4
    assert len(UGV_KINDS) == 4


@pytest.mark.parametrize(
    "kind",
    [
        CommandKind.UAV_PATROL,
        CommandKind.UAV_GOTO,
        CommandKind.UAV_RTL,
        CommandKind.UAV_HOLD,
    ],
)
def test_uav_kinds_set_membership(kind: CommandKind):
    assert kind in UAV_KINDS
    assert kind not in UGV_KINDS


@pytest.mark.parametrize(
    "kind",
    [
        CommandKind.UGV_GOTO,
        CommandKind.UGV_RTL,
        CommandKind.UGV_EXTINGUISH,
        CommandKind.UGV_STOP,
    ],
)
def test_ugv_kinds_set_membership(kind: CommandKind):
    assert kind in UGV_KINDS
    assert kind not in UAV_KINDS


def test_removed_kinds_are_gone():
    """Spec deletes UGV_DISPATCH / MARK_EVENT / ATTACH_ACTOR."""
    for legacy in ("UGV_DISPATCH", "MARK_EVENT", "ATTACH_ACTOR"):
        with pytest.raises(ValueError):
            CommandKind(legacy)


# ---- ParsedCommand --------------------------------------------------------


def test_parsed_command_has_params_not_payload():
    cmd = ParsedCommand(
        id="c1", kind=CommandKind.UAV_HOLD, target="UAV-01",
        params={"foo": "bar"},
    )
    assert cmd.params == {"foo": "bar"}
    # `payload` is no longer a field — should raise on access.
    with pytest.raises(AttributeError):
        cmd.payload  # type: ignore[attr-defined]


def test_parsed_command_defaults():
    cmd = ParsedCommand(id="c2", kind=CommandKind.UAV_HOLD, target="UAV-01")
    assert cmd.priority == "normal"
    assert cmd.params == {}


def test_parsed_command_params_default_is_independent_per_instance():
    a = ParsedCommand(id="a", kind=CommandKind.UAV_HOLD, target="UAV-01")
    b = ParsedCommand(id="b", kind=CommandKind.UAV_HOLD, target="UAV-02")
    a.params["x"] = 1
    assert b.params == {}


# ---- RejectCommand --------------------------------------------------------


def test_reject_command_minimal_reason_only():
    r = RejectCommand("parse_error")
    assert r.reason == "parse_error"
    assert r.detail == {}
    assert str(r) == "parse_error"


def test_reject_command_with_detail():
    r = RejectCommand("not_in_range", {"distance_m": 18.7, "max_m": 5.0})
    assert r.reason == "not_in_range"
    assert r.detail == {"distance_m": 18.7, "max_m": 5.0}
    assert "not_in_range" in str(r)
    assert "18.7" in str(r)


def test_reject_command_detail_copied_not_aliased():
    src = {"x": 1}
    r = RejectCommand("internal_error", src)
    src["x"] = 2
    assert r.detail == {"x": 1}


def test_reject_command_to_payload_matches_sio_call_shape():
    r = RejectCommand("kind_target_mismatch", {"kind": "UAV_GOTO", "target": "UGV-01"})
    payload = r.to_payload()
    assert payload == {
        "reason": "kind_target_mismatch",
        "detail": {"kind": "UAV_GOTO", "target": "UGV-01"},
    }


def test_reject_command_to_payload_detail_is_none_when_empty():
    r = RejectCommand("overloaded")
    assert r.to_payload() == {"reason": "overloaded", "detail": None}


def test_reject_command_is_an_exception():
    with pytest.raises(RejectCommand):
        raise RejectCommand("scenario_resetting")
