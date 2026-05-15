"""Projector tests — for_frontend / for_agent shapes against spec.md §8."""

from __future__ import annotations

from carlabridge.bus.projector import FocusBinding, for_agent, for_frontend
from carlabridge.core.fleet import Pose
from carlabridge.core.incident import Incident
from carlabridge.core.snapshot import (
    TrafficLightState,
    UavState,
    VehicleState,
    WorldSnapshot,
)


def _make_snap() -> WorldSnapshot:
    return WorldSnapshot(
        sim_time=12.5,
        traffic_lights=[
            TrafficLightState(id="TL-1", pose=(0, 0, 0), phase="red", remaining_s=4.0),
            TrafficLightState(id="TL-2", pose=(0, 0, 0), phase="green", remaining_s=2.0),
            TrafficLightState(id="TL-3", pose=(0, 0, 0), phase="red", remaining_s=1.0),
        ],
        vehicles=[
            VehicleState(
                id="UGV-01",
                role="dispatchable",
                pose=(10, 20, 0),
                yaw=15.0,
                speed=3.4,
                heading=15.0,
            ),
            VehicleState(
                id="VEH-99",
                role="civilian",
                pose=(50, 50, 0),
                yaw=180.0,
                speed=10.0,
                heading=180.0,
            ),
        ],
        uavs=[
            UavState(
                id="UAV-01",
                role="patrol",
                pose=(100, 200, 80),
                altitude=80,
                heading=45,
                battery=92.0,
                speed=15.0,
            ),
            UavState(
                id="UAV-02",
                role="standby",
                pose=(0, 0, 0),
                altitude=0,
                heading=0,
                battery=100,
            ),
        ],
    )


def test_for_agent_is_full_dict():
    snap = _make_snap()
    payload = for_agent(snap)
    assert payload["sim_time"] == 12.5
    assert len(payload["traffic_lights"]) == 3
    assert len(payload["vehicles"]) == 2
    assert len(payload["uavs"]) == 2


def test_for_frontend_default_focus_picks_first():
    snap = _make_snap()
    focus = FocusBinding()
    payload = for_frontend(snap, focus)
    assert payload["uav"]["id"] == "UAV-01"
    assert payload["uav"]["altitude"] == 80
    assert payload["uav"]["battery"] == 92.0
    assert "gps" in payload["uav"]
    assert payload["ugv"]["id"] == "UGV-01"
    assert payload["ugv"]["obstacle"] == "safe"
    assert payload["city"]["vehicles"] == 2


def test_for_frontend_explicit_focus():
    snap = _make_snap()
    focus = FocusBinding(uav="UAV-02", ugv="UGV-01")
    payload = for_frontend(snap, focus)
    assert payload["uav"]["id"] == "UAV-02"
    assert payload["ugv"]["id"] == "UGV-01"


def test_for_frontend_city_intersections_congested():
    """≥30% of traffic lights on red → 'congested'."""
    snap = _make_snap()  # 2 of 3 red → 67%
    payload = for_frontend(snap, FocusBinding())
    assert payload["city"]["intersections"] == "congested"


def test_for_frontend_omits_missing_focus():
    """If snapshot has no UAV/UGV, those keys are absent (partial merge on frontend)."""
    snap = WorldSnapshot(sim_time=0)
    payload = for_frontend(snap, FocusBinding())
    assert "uav" not in payload
    assert "ugv" not in payload
    assert payload["city"]["vehicles"] == 0


def test_focus_binding_thread_safe_setters():
    fb = FocusBinding()
    fb.set_uav("UAV-09")
    fb.set_ugv("UGV-09")
    assert fb.snapshot() == ("UAV-09", "UGV-09")
    fb.set_uav(None)
    assert fb.snapshot() == (None, "UGV-09")


# ---- R2: for_agent v0.3 wire payload -------------------------------------


def _make_snap_v2() -> WorldSnapshot:
    return WorldSnapshot(
        sim_time=42.0,
        run_id=3,
        bridge_session_id="br-9c1a",
        traffic_lights=[],
        vehicles=[
            VehicleState(
                id="UGV-01", role="dispatchable", pose=(0, 0, 0),
                yaw=0, speed=0, heading=0,
            ),
        ],
        uavs=[
            UavState(id="UAV-01", role="patrol", pose=(0, 0, 60),
                     altitude=60, heading=0, battery=100),
        ],
        incidents=[
            Incident(id="fire-001", kind="fire",
                     position=Pose(90, 0, 0), severity="high",
                     since_sim_time=12.5),
        ],
        in_flight_commands=[
            {"cmd_id": "cmd-a", "kind": "UAV_PATROL", "target": "UAV-01",
             "accepted_at_sim_time": 1.0},
        ],
    )


def test_for_agent_v2_includes_all_design_4_1_fields():
    payload = for_agent(_make_snap_v2())
    expected = {
        "sim_time", "run_id", "bridge_session_id",
        "traffic_lights", "vehicles", "uavs",
        "incidents", "in_flight_commands",
    }
    assert expected <= set(payload.keys())


def test_for_agent_v2_incidents_have_xyz_only_position():
    payload = for_agent(_make_snap_v2())
    assert len(payload["incidents"]) == 1
    inc = payload["incidents"][0]
    # Pose euler must not leak through.
    assert set(inc["position"].keys()) == {"x", "y", "z"}
    assert inc["id"] == "fire-001"
    assert inc["severity"] == "high"
    assert inc["since_sim_time"] == 12.5


def test_for_agent_v2_in_flight_commands_passed_through_in_order():
    payload = for_agent(_make_snap_v2())
    assert [c["cmd_id"] for c in payload["in_flight_commands"]] == ["cmd-a"]


def test_for_agent_v2_field_order_is_stable():
    payload = for_agent(_make_snap_v2())
    assert list(payload.keys()) == [
        "sim_time",
        "run_id",
        "bridge_session_id",
        "traffic_lights",
        "vehicles",
        "uavs",
        "incidents",
        "in_flight_commands",
    ]


def test_for_agent_v2_run_id_and_session_threaded_through():
    payload = for_agent(_make_snap_v2())
    assert payload["run_id"] == 3
    assert payload["bridge_session_id"] == "br-9c1a"


def test_for_agent_v2_empty_defaults():
    """A snapshot that never had R2 fields supplied still projects cleanly."""
    snap = WorldSnapshot(sim_time=0)
    payload = for_agent(snap)
    assert payload["run_id"] == 0
    assert payload["bridge_session_id"] == ""
    assert payload["incidents"] == []
    assert payload["in_flight_commands"] == []
