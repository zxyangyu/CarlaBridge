"""Unit tests for Fleet, VirtualMember, Pose."""

from __future__ import annotations

import pytest

from carlabridge.core.fleet import Fleet, Pose, VirtualMember
from carlabridge.core.incident import Incident


def test_pose_distance():
    a = Pose(0, 0, 0)
    b = Pose(3, 4, 0)
    assert a.distance_to(b) == pytest.approx(5.0)


def test_pose_lerp_partial():
    a = Pose(0, 0, 0)
    b = Pose(10, 0, 0)
    moved = a.lerp_toward(b, 3.0)
    assert moved.x == pytest.approx(3.0)
    assert moved.y == 0
    assert moved.z == 0


def test_pose_lerp_snaps_when_within_step():
    a = Pose(0, 0, 0)
    b = Pose(2, 0, 0)
    moved = a.lerp_toward(b, 10.0)
    # Within step → snap to target.
    assert (moved.x, moved.y, moved.z) == (b.x, b.y, b.z)


def test_virtual_member_step_moves_toward_target():
    uav = VirtualMember(
        entity_id="UAV-01",
        role="patrol",
        _pose=Pose(0, 0, 50),
        cruise_speed=10.0,
    )
    uav.set_target(Pose(100, 0, 50))
    uav.step(dt=0.5)  # 5 m of motion
    assert uav.pose().x == pytest.approx(5.0)
    assert uav.target is not None


def test_virtual_member_step_arrives_and_clears_target():
    uav = VirtualMember(
        entity_id="UAV-01",
        role="patrol",
        _pose=Pose(0, 0, 0),
        cruise_speed=10.0,
    )
    uav.set_target(Pose(2, 0, 0))
    uav.step(dt=1.0)  # would move 10 m but target only 2 m away → snap
    assert uav.target is None
    assert uav.pose().x == pytest.approx(2.0)


def test_virtual_member_no_target_is_noop():
    uav = VirtualMember(entity_id="UAV-01", role="standby", _pose=Pose(7, 8, 9))
    uav.step(dt=0.1)
    p = uav.pose()
    assert (p.x, p.y, p.z) == (7, 8, 9)


def test_fleet_register_and_get():
    f = Fleet()
    m = VirtualMember(entity_id="UAV-01", role="patrol")
    f.register(m)
    assert f.get("UAV-01") is m
    assert len(f) == 1


def test_fleet_register_duplicate_raises():
    f = Fleet()
    f.register(VirtualMember(entity_id="UAV-01", role="patrol"))
    with pytest.raises(ValueError):
        f.register(VirtualMember(entity_id="UAV-01", role="patrol"))


def test_fleet_unregister():
    f = Fleet()
    m = VirtualMember(entity_id="UAV-01", role="patrol")
    f.register(m)
    assert f.unregister("UAV-01") is m
    assert f.unregister("UAV-01") is None
    assert len(f) == 0


def test_fleet_by_role_and_virtual_filter():
    f = Fleet()
    f.register(VirtualMember(entity_id="UAV-01", role="patrol"))
    f.register(VirtualMember(entity_id="UAV-02", role="patrol"))
    f.register(VirtualMember(entity_id="UAV-03", role="standby"))
    patrol = f.by_role("patrol")
    assert {m.entity_id for m in patrol} == {"UAV-01", "UAV-02"}
    assert len(f.virtual()) == 3
    assert len(f.carla_members()) == 0


def test_fleet_clear():
    f = Fleet()
    f.register(VirtualMember(entity_id="UAV-01", role="patrol"))
    f.register(VirtualMember(entity_id="UAV-02", role="patrol"))
    f.clear()
    assert len(f) == 0


# ---- origins (R2) --------------------------------------------------------


def test_fleet_origin_set_and_get():
    f = Fleet()
    f.register(VirtualMember(entity_id="UAV-01", role="patrol"))
    f.set_origin("UAV-01", Pose(x=-20, y=0, z=60))
    o = f.get_origin("UAV-01")
    assert o is not None
    assert (o.x, o.y, o.z) == (-20, 0, 60)


def test_fleet_get_origin_missing_returns_none():
    f = Fleet()
    assert f.get_origin("UAV-99") is None


def test_fleet_origins_snapshot_is_a_copy():
    f = Fleet()
    f.set_origin("UAV-01", Pose(0, 0, 60))
    snap = f.origins()
    snap["UAV-99"] = Pose(9, 9, 9)
    # Mutating the snapshot must not leak into Fleet.
    assert f.get_origin("UAV-99") is None


def test_fleet_register_does_not_implicitly_set_origin():
    f = Fleet()
    f.register(VirtualMember(entity_id="UAV-01", role="patrol", _pose=Pose(5, 6, 7)))
    assert f.get_origin("UAV-01") is None


def test_fleet_unregister_drops_origin():
    f = Fleet()
    f.register(VirtualMember(entity_id="UAV-01", role="patrol"))
    f.set_origin("UAV-01", Pose(1, 2, 3))
    f.unregister("UAV-01")
    assert f.get_origin("UAV-01") is None


def test_fleet_clear_drops_origins():
    f = Fleet()
    f.set_origin("UAV-01", Pose())
    f.set_origin("UGV-01", Pose())
    f.clear()
    assert f.origins() == {}


# ---- incidents (R2) ------------------------------------------------------


def _mk_incident(iid: str = "fire-001", since: float = 12.5) -> Incident:
    return Incident(
        id=iid, kind="fire", position=Pose(x=90, y=0, z=0),
        severity="high", since_sim_time=since,
    )


def test_fleet_add_incident_and_get():
    f = Fleet()
    inc = _mk_incident()
    f.add_incident(inc)
    assert f.get_incident("fire-001") is inc


def test_fleet_add_incident_duplicate_raises():
    f = Fleet()
    f.add_incident(_mk_incident("fire-001"))
    with pytest.raises(ValueError):
        f.add_incident(_mk_incident("fire-001"))


def test_fleet_remove_incident_returns_removed_or_none():
    f = Fleet()
    inc = _mk_incident("fire-001")
    f.add_incident(inc)
    assert f.remove_incident("fire-001") is inc
    assert f.remove_incident("fire-001") is None


def test_fleet_clear_incidents():
    f = Fleet()
    f.add_incident(_mk_incident("fire-001"))
    f.add_incident(_mk_incident("fire-002"))
    f.clear_incidents()
    assert f.incidents() == {}


def test_fleet_incidents_snapshot_is_a_copy():
    f = Fleet()
    f.add_incident(_mk_incident("fire-001"))
    snap = f.incidents()
    snap.pop("fire-001")
    # Mutating the snapshot must not leak into Fleet.
    assert f.get_incident("fire-001") is not None


def test_fleet_clear_also_drops_incidents():
    f = Fleet()
    f.add_incident(_mk_incident("fire-001"))
    f.clear()
    assert f.incidents() == {}
