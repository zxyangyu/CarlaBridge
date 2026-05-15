"""dispatcher.parse — schema + per-kind validators (R1)."""

from __future__ import annotations

import pytest

from carlabridge.commands.dispatcher import parse
from carlabridge.commands.enum import CommandKind, RejectCommand


# ---- envelope-level errors -----------------------------------------------


def test_parse_rejects_non_dict_payload():
    with pytest.raises(RejectCommand) as ei:
        parse("not a dict")  # type: ignore[arg-type]
    assert ei.value.reason == "parse_error"


def test_parse_rejects_missing_id():
    with pytest.raises(RejectCommand) as ei:
        parse({"kind": "UAV_HOLD", "target": "UAV-01"})
    assert ei.value.reason == "parse_error"
    assert ei.value.detail.get("field") == "id"


def test_parse_rejects_missing_kind():
    with pytest.raises(RejectCommand) as ei:
        parse({"id": "c1", "target": "UAV-01"})
    assert ei.value.reason == "parse_error"
    assert ei.value.detail.get("field") == "kind"


def test_parse_rejects_unknown_kind():
    with pytest.raises(RejectCommand) as ei:
        parse({"id": "c1", "kind": "NUKE_FROM_ORBIT", "target": "UAV-01"})
    assert ei.value.reason == "parse_error"
    assert ei.value.detail.get("field") == "kind"


def test_parse_rejects_missing_target():
    with pytest.raises(RejectCommand) as ei:
        parse({"id": "c1", "kind": "UAV_HOLD"})
    assert ei.value.reason == "parse_error"
    assert ei.value.detail.get("field") == "target"


def test_parse_rejects_non_string_priority():
    with pytest.raises(RejectCommand) as ei:
        parse({
            "id": "c1", "kind": "UAV_HOLD", "target": "UAV-01", "priority": 9,
        })
    assert ei.value.reason == "parse_error"
    assert ei.value.detail.get("field") == "priority"


def test_parse_rejects_non_dict_params():
    with pytest.raises(RejectCommand) as ei:
        parse({
            "id": "c1", "kind": "UAV_HOLD", "target": "UAV-01", "params": "x",
        })
    assert ei.value.reason == "parse_error"
    assert ei.value.detail.get("field") == "params"


def test_parse_accepts_legacy_text_field_for_kind():
    cmd = parse({"id": "c1", "text": "UAV_HOLD", "target": "UAV-01"})
    assert cmd.kind == CommandKind.UAV_HOLD


def test_parse_accepts_legacy_payload_field_for_params():
    cmd = parse({
        "id": "c1", "kind": "UAV_HOLD", "target": "UAV-01",
        "payload": {"k": "v"},
    })
    assert cmd.params == {"k": "v"}


def test_parse_priority_default_normal():
    cmd = parse({"id": "c1", "kind": "UAV_HOLD", "target": "UAV-01"})
    assert cmd.priority == "normal"


# ---- kind / target family mismatch ---------------------------------------


def test_parse_uav_kind_with_ugv_target_rejects_with_canonical_reason():
    with pytest.raises(RejectCommand) as ei:
        parse({
            "id": "c1", "kind": "UAV_HOLD", "target": "UGV-01",
        })
    assert ei.value.reason == "kind_target_mismatch"


def test_parse_ugv_kind_with_uav_target_rejects_with_canonical_reason():
    with pytest.raises(RejectCommand) as ei:
        parse({
            "id": "c1", "kind": "UGV_STOP", "target": "UAV-02",
        })
    assert ei.value.reason == "kind_target_mismatch"


# ---- UAV_PATROL ----------------------------------------------------------


def test_uav_patrol_minimal_ok():
    cmd = parse({
        "id": "c1", "kind": "UAV_PATROL", "target": "UAV-01",
        "params": {
            "path": [{"x": 0, "y": 0, "z": 60}],
            "cruise_speed": 8.0,
        },
    })
    assert cmd.kind == CommandKind.UAV_PATROL
    assert cmd.params.get("loop", False) is False


def test_uav_patrol_with_loop_true_ok():
    cmd = parse({
        "id": "c1", "kind": "UAV_PATROL", "target": "UAV-01",
        "params": {
            "path": [{"x": 0, "y": 0, "z": 60}, {"x": 10, "y": 0, "z": 60}],
            "cruise_speed": 8.0,
            "loop": True,
        },
    })
    assert cmd.params["loop"] is True


def test_uav_patrol_empty_path_rejects():
    with pytest.raises(RejectCommand) as ei:
        parse({
            "id": "c1", "kind": "UAV_PATROL", "target": "UAV-01",
            "params": {"path": [], "cruise_speed": 8.0},
        })
    assert ei.value.reason == "parse_error"


def test_uav_patrol_bad_waypoint_rejects():
    with pytest.raises(RejectCommand):
        parse({
            "id": "c1", "kind": "UAV_PATROL", "target": "UAV-01",
            "params": {"path": [{"x": 0, "y": 0}], "cruise_speed": 8.0},
        })


def test_uav_patrol_missing_cruise_speed_rejects():
    with pytest.raises(RejectCommand):
        parse({
            "id": "c1", "kind": "UAV_PATROL", "target": "UAV-01",
            "params": {"path": [{"x": 0, "y": 0, "z": 60}]},
        })


# ---- UAV_GOTO ------------------------------------------------------------


def test_uav_goto_ok():
    cmd = parse({
        "id": "c1", "kind": "UAV_GOTO", "target": "UAV-02",
        "params": {"waypoint": {"x": 10, "y": 0, "z": 60}, "cruise_speed": 8.0},
    })
    assert cmd.kind == CommandKind.UAV_GOTO


def test_uav_goto_missing_waypoint_rejects():
    with pytest.raises(RejectCommand):
        parse({
            "id": "c1", "kind": "UAV_GOTO", "target": "UAV-02",
            "params": {"cruise_speed": 8.0},
        })


def test_uav_goto_zero_speed_rejects():
    with pytest.raises(RejectCommand):
        parse({
            "id": "c1", "kind": "UAV_GOTO", "target": "UAV-02",
            "params": {"waypoint": {"x": 0, "y": 0, "z": 60}, "cruise_speed": 0},
        })


# ---- UAV_RTL -------------------------------------------------------------


def test_uav_rtl_no_params_ok():
    cmd = parse({"id": "c1", "kind": "UAV_RTL", "target": "UAV-03"})
    assert cmd.kind == CommandKind.UAV_RTL


def test_uav_rtl_with_cruise_speed_ok():
    cmd = parse({
        "id": "c1", "kind": "UAV_RTL", "target": "UAV-03",
        "params": {"cruise_speed": 12.0},
    })
    assert cmd.params["cruise_speed"] == 12.0


def test_uav_rtl_negative_speed_rejects():
    with pytest.raises(RejectCommand):
        parse({
            "id": "c1", "kind": "UAV_RTL", "target": "UAV-03",
            "params": {"cruise_speed": -5},
        })


# ---- UAV_HOLD ------------------------------------------------------------


def test_uav_hold_no_params_ok():
    cmd = parse({"id": "c1", "kind": "UAV_HOLD", "target": "UAV-01"})
    assert cmd.kind == CommandKind.UAV_HOLD


def test_uav_hold_ignores_extra_params():
    cmd = parse({
        "id": "c1", "kind": "UAV_HOLD", "target": "UAV-01",
        "params": {"junk": 1},
    })
    assert cmd.params == {"junk": 1}


# ---- UGV_GOTO ------------------------------------------------------------


def test_ugv_goto_ok():
    cmd = parse({
        "id": "c1", "kind": "UGV_GOTO", "target": "UGV-01",
        "params": {"dest": {"x": 90, "y": 0, "z": 0}},
    })
    assert cmd.kind == CommandKind.UGV_GOTO


def test_ugv_goto_with_target_speed_ok():
    cmd = parse({
        "id": "c1", "kind": "UGV_GOTO", "target": "UGV-01",
        "params": {"dest": {"x": 90, "y": 0, "z": 0}, "target_speed": 30.0},
    })
    assert cmd.params["target_speed"] == 30.0


def test_ugv_goto_missing_dest_rejects():
    with pytest.raises(RejectCommand):
        parse({
            "id": "c1", "kind": "UGV_GOTO", "target": "UGV-01",
            "params": {},
        })


def test_ugv_goto_bad_target_speed_rejects():
    with pytest.raises(RejectCommand):
        parse({
            "id": "c1", "kind": "UGV_GOTO", "target": "UGV-01",
            "params": {"dest": {"x": 1, "y": 2, "z": 3}, "target_speed": 0},
        })


# ---- UGV_RTL -------------------------------------------------------------


def test_ugv_rtl_no_params_ok():
    cmd = parse({"id": "c1", "kind": "UGV_RTL", "target": "UGV-01"})
    assert cmd.kind == CommandKind.UGV_RTL


def test_ugv_rtl_with_target_speed_ok():
    cmd = parse({
        "id": "c1", "kind": "UGV_RTL", "target": "UGV-01",
        "params": {"target_speed": 20.0},
    })
    assert cmd.params["target_speed"] == 20.0


def test_ugv_rtl_zero_target_speed_rejects():
    with pytest.raises(RejectCommand):
        parse({
            "id": "c1", "kind": "UGV_RTL", "target": "UGV-01",
            "params": {"target_speed": 0},
        })


# ---- UGV_EXTINGUISH ------------------------------------------------------


def test_ugv_extinguish_ok():
    cmd = parse({
        "id": "c1", "kind": "UGV_EXTINGUISH", "target": "UGV-01",
        "params": {"incident_id": "fire-001"},
    })
    assert cmd.params["incident_id"] == "fire-001"


def test_ugv_extinguish_missing_incident_id_rejects():
    with pytest.raises(RejectCommand) as ei:
        parse({
            "id": "c1", "kind": "UGV_EXTINGUISH", "target": "UGV-01",
            "params": {},
        })
    assert ei.value.reason == "parse_error"
    assert ei.value.detail.get("field") == "incident_id"


def test_ugv_extinguish_empty_incident_id_rejects():
    with pytest.raises(RejectCommand):
        parse({
            "id": "c1", "kind": "UGV_EXTINGUISH", "target": "UGV-01",
            "params": {"incident_id": ""},
        })


# ---- UGV_STOP ------------------------------------------------------------


def test_ugv_stop_ok():
    cmd = parse({"id": "c1", "kind": "UGV_STOP", "target": "UGV-01"})
    assert cmd.kind == CommandKind.UGV_STOP


def test_ugv_stop_ignores_extra_params():
    cmd = parse({
        "id": "c1", "kind": "UGV_STOP", "target": "UGV-01",
        "params": {"unrelated": True},
    })
    assert cmd.kind == CommandKind.UGV_STOP


# ---- removed legacy kinds ------------------------------------------------


@pytest.mark.parametrize("legacy", ["UGV_DISPATCH", "MARK_EVENT", "ATTACH_ACTOR"])
def test_parse_rejects_removed_legacy_kinds(legacy: str):
    with pytest.raises(RejectCommand) as ei:
        parse({"id": "c1", "kind": legacy, "target": "UGV-01"})
    assert ei.value.reason == "parse_error"
