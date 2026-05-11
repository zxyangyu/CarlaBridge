"""SnapshotBuilder tests using FakeWorld (no CARLA required)."""

from __future__ import annotations

from carlabridge.core.fleet import CarlaActorMember, Fleet, Pose, VirtualMember
from carlabridge.core.snapshot import SnapshotBuilder, WorldSnapshot
from tests.fakes.fake_world import FakeActor, FakeTrafficLight, FakeWorld


def _build_world() -> FakeWorld:
    w = FakeWorld()
    w.add_traffic_light(FakeTrafficLight(11, 10.0, 20.0, 0.5, state="Red", red_time=10, elapsed=4))
    w.add_traffic_light(FakeTrafficLight(12, 30.0, 40.0, 0.5, state="Green", green_time=12, elapsed=2))
    return w


def test_empty_world_snapshot_has_sim_time():
    builder = SnapshotBuilder(world=FakeWorld())
    snap = builder.build(Fleet(), sim_time=1.23)
    assert isinstance(snap, WorldSnapshot)
    assert snap.sim_time == 1.23
    assert snap.traffic_lights == []
    assert snap.vehicles == []
    assert snap.uavs == []


def test_traffic_lights_projected():
    w = _build_world()
    builder = SnapshotBuilder(world=w)
    snap = builder.build(Fleet(), sim_time=0.0)
    assert len(snap.traffic_lights) == 2
    ids = {t.id for t in snap.traffic_lights}
    assert ids == {"TL-11", "TL-12"}
    by_id = {t.id: t for t in snap.traffic_lights}
    assert by_id["TL-11"].phase == "red"
    assert by_id["TL-11"].remaining_s == 6.0  # 10 - 4
    assert by_id["TL-12"].phase == "green"
    assert by_id["TL-12"].remaining_s == 10.0


def test_vehicles_from_fleet():
    w = FakeWorld()
    fleet = Fleet()
    actor = FakeActor(100, x=1.0, y=2.0, z=0.0, yaw=45.0, vx=3.0, vy=4.0)  # speed = 5
    fleet.register(CarlaActorMember(entity_id="UGV-01", role="dispatchable", actor=actor))
    builder = SnapshotBuilder(world=w)
    snap = builder.build(fleet, sim_time=0.0)
    assert len(snap.vehicles) == 1
    v = snap.vehicles[0]
    assert v.id == "UGV-01"
    assert v.role == "dispatchable"
    assert v.pose == (1.0, 2.0, 0.0)
    assert v.yaw == 45.0
    assert v.heading == 45.0
    assert abs(v.speed - 5.0) < 1e-6


def test_uavs_from_fleet_virtual():
    fleet = Fleet()
    uav = VirtualMember(
        entity_id="UAV-01",
        role="patrol",
        _pose=Pose(x=10, y=20, z=50, yaw=90),
        altitude=50,
        heading=90,
        battery=88.5,
    )
    uav.set_target(Pose(x=100, y=20, z=50, yaw=90), cruise_speed=20.0)
    fleet.register(uav)
    snap = SnapshotBuilder(world=FakeWorld()).build(fleet, sim_time=0.0)
    assert len(snap.uavs) == 1
    u = snap.uavs[0]
    assert u.id == "UAV-01"
    assert u.role == "patrol"
    assert u.altitude == 50
    assert u.heading == 90
    assert u.battery == 88.5
    assert u.speed == 20.0  # moving toward target

    # When target cleared, speed reports 0.
    uav.set_target(None)
    snap = SnapshotBuilder(world=FakeWorld()).build(fleet, sim_time=0.0)
    assert snap.uavs[0].speed == 0.0


def test_snapshot_to_dict_jsonable():
    """to_dict() output must contain only JSON-friendly primitives."""
    fleet = Fleet()
    fleet.register(VirtualMember(entity_id="UAV-01", role="standby", battery=100.0))
    w = _build_world()
    snap = SnapshotBuilder(world=w).build(fleet, sim_time=1.5)
    d = snap.to_dict()
    assert d["sim_time"] == 1.5
    assert isinstance(d["traffic_lights"], list)
    assert isinstance(d["uavs"], list)
    assert d["uavs"][0]["id"] == "UAV-01"
    # Tuples become lists in asdict — both are JSON-serializable.
    assert isinstance(d["traffic_lights"][0]["pose"], (tuple, list))


def test_refresh_lights_rescans():
    w = FakeWorld()
    builder = SnapshotBuilder(world=w)
    snap = builder.build(Fleet(), sim_time=0)
    assert snap.traffic_lights == []
    w.add_traffic_light(FakeTrafficLight(1, 0, 0, 0))
    # Cache is non-empty (empty list cached as []), so first build re-scans
    # whenever cache is empty — verify by calling refresh_lights to be sure.
    builder.refresh_lights()
    snap = builder.build(Fleet(), sim_time=0)
    assert len(snap.traffic_lights) == 1
