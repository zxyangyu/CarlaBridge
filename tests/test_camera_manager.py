"""CameraManager: bind / spawn_all / rebind / update_followers / detach_all.

We inject a fake spawner so no CARLA actor is created. The fake records each
call and produces a SpawnedCamera whose `.actor` is a FakeSensor — its
`stop()/destroy()/set_transform()` methods just record their calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from carlabridge.core.fleet import CarlaActorMember, Fleet, Pose, VirtualMember
from carlabridge.sensors.camera import (
    CameraBinding,
    CameraManager,
    CameraSpec,
    SpawnedCamera,
)


# ---------- fakes ---------------------------------------------------------


@dataclass
class FakeSensor:
    """Stand-in for a CARLA Sensor returned by spawn_actor."""

    id: int = 0
    stopped: bool = False
    destroyed: bool = False
    transforms: list = field(default_factory=list)

    def stop(self) -> None:
        self.stopped = True

    def destroy(self) -> None:
        self.destroyed = True

    def set_transform(self, transform) -> None:
        # In production, `transform` is a `carla.Transform`. The test passes it
        # straight through from update_followers, so we just record.
        self.transforms.append(transform)


class FakeSpawner:
    """Callable matching SpawnerFn. Records every spawn call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.next_id = 100

    def __call__(self, world, spec: CameraSpec, queue, attach_to):  # noqa: ARG002
        sensor = FakeSensor(id=self.next_id)
        self.next_id += 1
        self.calls.append(
            {
                "spec_id": spec.id,
                "mode": spec.mode,
                "x": spec.x,
                "y": spec.y,
                "z": spec.z,
                "attach_entity_id": spec.attach_entity_id,
                "attach_to_id": getattr(attach_to, "id", None),
                "sensor_id": sensor.id,
            }
        )
        return SpawnedCamera(spec=spec, actor=sensor, queue=queue)


class FakeCarlaActor:
    """Stand-in for a CARLA vehicle actor (only id is read by spawner)."""

    def __init__(self, actor_id: int = 1):
        self.id = actor_id

    def get_transform(self):  # not used in CameraManager tests
        raise NotImplementedError


# ---------- helpers -------------------------------------------------------


def _city_spec() -> CameraSpec:
    return CameraSpec(
        id="city", mode="world_pose",
        x=0, y=0, z=200, pitch=-90, yaw=0, roll=0,
    )


def _aerial_spec(target: str | None = None) -> CameraSpec:
    return CameraSpec(
        id="aerial", mode="follows_virtual",
        x=0, y=0, z=20, pitch=-30,
        attach_entity_id=target,
    )


def _ground_spec(target: str | None = None) -> CameraSpec:
    return CameraSpec(
        id="ground", mode="attached_to_actor",
        x=-3, y=0, z=2, pitch=-10,
        attach_entity_id=target,
    )


# ---------- tests ---------------------------------------------------------


def test_bind_creates_queue():
    mgr = CameraManager(spawner=FakeSpawner())
    mgr.bind(CameraBinding(spec=_city_spec()))
    assert mgr.queue_for("city") is not None
    assert mgr.get_binding("city") is not None
    assert "city" not in mgr.cameras  # not spawned yet


def test_spawn_all_world_pose():
    spawner = FakeSpawner()
    mgr = CameraManager(spawner=spawner)
    mgr.bind(CameraBinding(spec=_city_spec()))
    mgr.spawn_all(world=object(), fleet=Fleet())
    assert "city" in mgr.cameras
    assert len(spawner.calls) == 1
    assert spawner.calls[0]["spec_id"] == "city"
    assert spawner.calls[0]["attach_to_id"] is None


def test_spawn_all_skips_unfilled_attach_bindings():
    """aerial/ground without attach_entity_id should NOT be spawned (waits for M5)."""
    spawner = FakeSpawner()
    mgr = CameraManager(spawner=spawner)
    mgr.bind(CameraBinding(spec=_aerial_spec(None)))
    mgr.bind(CameraBinding(spec=_ground_spec(None)))
    mgr.spawn_all(world=object(), fleet=Fleet())
    assert spawner.calls == []
    assert mgr.cameras == {}


def test_attached_to_actor_passes_carla_actor():
    spawner = FakeSpawner()
    mgr = CameraManager(spawner=spawner)
    mgr.bind(CameraBinding(spec=_ground_spec("UGV-01")))
    fleet = Fleet()
    actor = FakeCarlaActor(actor_id=42)
    fleet.register(CarlaActorMember(entity_id="UGV-01", role="dispatchable", actor=actor))
    mgr.spawn_all(world=object(), fleet=fleet)
    assert "ground" in mgr.cameras
    assert spawner.calls[0]["attach_to_id"] == 42


def test_attached_to_actor_rejects_virtual_target():
    spawner = FakeSpawner()
    mgr = CameraManager(spawner=spawner)
    mgr.bind(CameraBinding(spec=_ground_spec("UAV-01")))
    fleet = Fleet()
    fleet.register(VirtualMember(entity_id="UAV-01", role="patrol"))
    mgr.spawn_all(world=object(), fleet=fleet)
    # spawn_all swallows the per-channel exception — no camera registered.
    assert "ground" not in mgr.cameras


def test_follows_virtual_spawns_at_target_pose_plus_offset():
    spawner = FakeSpawner()
    mgr = CameraManager(spawner=spawner)
    mgr.bind(CameraBinding(spec=_aerial_spec("UAV-01")))
    fleet = Fleet()
    uav = VirtualMember(
        entity_id="UAV-01", role="patrol",
        _pose=Pose(x=100, y=200, z=50, yaw=30),
        altitude=50, heading=30, battery=90,
    )
    fleet.register(uav)
    mgr.spawn_all(world=object(), fleet=fleet)
    call = spawner.calls[0]
    # offset z=20 + uav z=50 → 70; yaw composes too
    assert call["x"] == 100
    assert call["y"] == 200
    assert call["z"] == 70
    assert call["attach_to_id"] is None  # follows_virtual is NOT attached in CARLA


def test_rebind_destroys_and_respawns_with_same_queue():
    spawner = FakeSpawner()
    mgr = CameraManager(spawner=spawner)
    mgr.bind(CameraBinding(spec=_ground_spec("UGV-01")))
    fleet = Fleet()
    a1 = FakeCarlaActor(actor_id=1)
    a2 = FakeCarlaActor(actor_id=2)
    fleet.register(CarlaActorMember(entity_id="UGV-01", role="dispatchable", actor=a1))
    fleet.register(CarlaActorMember(entity_id="UGV-02", role="dispatchable", actor=a2))
    mgr.spawn_all(world=object(), fleet=fleet)
    queue_before = mgr.queue_for("ground")
    old_cam = mgr.cameras["ground"]

    new_cam = mgr.rebind("ground", "UGV-02", world=object(), fleet=fleet)
    assert new_cam is not None
    # Old sensor torn down.
    assert old_cam.actor.stopped is True
    assert old_cam.actor.destroyed is True
    # New sensor attached to actor 2.
    assert spawner.calls[-1]["attach_to_id"] == 2
    # FrameQueue identity preserved — WebRTC track keeps working.
    assert mgr.queue_for("ground") is queue_before


def test_rebind_unknown_channel_returns_none():
    mgr = CameraManager(spawner=FakeSpawner())
    out = mgr.rebind("nope", "X", world=object(), fleet=Fleet())
    assert out is None


def test_rebind_world_pose_is_noop_keeps_camera():
    spawner = FakeSpawner()
    mgr = CameraManager(spawner=spawner)
    mgr.bind(CameraBinding(spec=_city_spec()))
    mgr.spawn_all(world=object(), fleet=Fleet())
    cam_before = mgr.cameras["city"]
    out = mgr.rebind("city", "anything", world=object(), fleet=Fleet())
    assert out is cam_before  # nothing changed


def test_update_followers_sets_transform_each_call():
    spawner = FakeSpawner()
    mgr = CameraManager(spawner=spawner)
    mgr.bind(CameraBinding(spec=_aerial_spec("UAV-01")))
    fleet = Fleet()
    uav = VirtualMember(
        entity_id="UAV-01", role="patrol",
        _pose=Pose(x=0, y=0, z=50),
        altitude=50, heading=0, battery=90,
    )
    fleet.register(uav)
    mgr.spawn_all(world=object(), fleet=fleet)
    sensor: FakeSensor = mgr.cameras["aerial"].actor  # type: ignore[assignment]
    sensor.transforms.clear()
    # Move UAV; verify update_followers writes a transform.
    uav.set_pose(Pose(x=10, y=20, z=55))
    mgr.update_followers(fleet)
    assert len(sensor.transforms) == 1
    tf = sensor.transforms[0]
    # The transform is a carla.Transform — verify world location matches.
    assert tf.location.x == pytest.approx(10.0)
    assert tf.location.y == pytest.approx(20.0)
    assert tf.location.z == pytest.approx(75.0)  # 55 + 20 offset


def test_update_followers_skips_non_follower_modes():
    spawner = FakeSpawner()
    mgr = CameraManager(spawner=spawner)
    mgr.bind(CameraBinding(spec=_city_spec()))  # world_pose
    mgr.spawn_all(world=object(), fleet=Fleet())
    sensor: FakeSensor = mgr.cameras["city"].actor  # type: ignore[assignment]
    mgr.update_followers(Fleet())
    assert sensor.transforms == []


def test_spawn_camera_signature_matches_spawner_protocol():
    """Regression guard: SpawnerFn calls spawn_camera positionally with
    (world, spec, queue, attach_to). If attach_to becomes keyword-only the
    real-CARLA path breaks but FakeSpawner tests pass — so we check the sig
    directly here.
    """
    import inspect

    from carlabridge.sensors.camera import spawn_camera

    sig = inspect.signature(spawn_camera)
    params = list(sig.parameters.values())
    # First 4 params must accept positional (POSITIONAL_OR_KEYWORD or POSITIONAL_ONLY).
    for i, p in enumerate(params[:4]):
        assert p.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        ), f"param #{i} ({p.name}) must be positional, got {p.kind}"


def test_detach_all_clears():
    spawner = FakeSpawner()
    mgr = CameraManager(spawner=spawner)
    mgr.bind(CameraBinding(spec=_city_spec()))
    mgr.spawn_all(world=object(), fleet=Fleet())
    sensor: FakeSensor = mgr.cameras["city"].actor  # type: ignore[assignment]
    mgr.detach_all()
    assert mgr.cameras == {}
    assert sensor.stopped and sensor.destroyed
