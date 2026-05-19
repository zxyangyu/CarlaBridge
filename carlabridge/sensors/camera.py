"""CARLA camera lifecycle.

Three binding modes (design §7):

| mode               | how                                                   |
|--------------------|-------------------------------------------------------|
| world_pose         | spawn at given transform, no attach (used for `city`) |
| attached_to_actor  | spawn with `attach_to=actor` (used for `ground`)      |
| follows_virtual    | spawn unattached; tick post-hook drives set_transform |
|                    | from VirtualMember pose+offset (used for `aerial`)    |

The sensor callback runs on CARLA's internal thread — it MUST be cheap. The
listener wraps the raw buffer as numpy (zero-copy view), copies once into a
fresh ndarray (CARLA reuses its buffer), and pushes into FrameQueue.set_latest.

Lifecycle:
1. main creates a CameraManager and `bind()` channels (id + spec + target).
2. After CARLA `switch_to_sync`, call `spawn_all(world, fleet)`.
3. Each tick (post): `update_followers(fleet)` keeps follows_virtual cameras
   tracking their VirtualMember.
4. `rebind(channel_id, new_entity_id)` destroys + respawns the sensor while
   keeping the same FrameQueue (WebRTC track keeps producing).
5. `detach_all()` on shutdown — BEFORE CARLA async-mode restore.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Callable, Literal

import numpy as np

from carlabridge.core.fleet import CarlaActorMember, Fleet, Pose, VirtualMember
from carlabridge.sensors.frame_queue import FrameQueue

if TYPE_CHECKING:  # pragma: no cover
    import carla

log = logging.getLogger(__name__)

CameraMode = Literal["world_pose", "attached_to_actor", "follows_virtual"]


@dataclass(slots=True)
class CameraSpec:
    id: str  # channel id: 'aerial' | 'ground' | 'city'
    mode: CameraMode
    # World-frame pose for `world_pose`; offset for the others (world frame for
    # follows_virtual, actor-local frame for attached_to_actor — CARLA computes
    # the attach automatically).
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0
    roll: float = 0.0
    fov: float = 90.0
    width: int = 1280
    height: int = 720
    fps: int = 25
    # Only meaningful for attached_to_actor / follows_virtual.
    attach_entity_id: str | None = None
    blueprint: str = "sensor.camera.rgb"


@dataclass(slots=True)
class SpawnedCamera:
    """Handle returned by spawn_camera — keep until detach()."""

    spec: CameraSpec
    actor: "carla.Sensor"
    queue: FrameQueue

    def set_transform(self, transform: "carla.Transform") -> None:
        try:
            self.actor.set_transform(transform)
        except Exception:
            log.exception("camera %s set_transform failed", self.spec.id)

    def detach(self) -> None:
        try:
            self.actor.stop()
        except Exception:  # pragma: no cover -- best-effort
            log.exception("camera %s stop() failed", self.spec.id)
        try:
            self.actor.destroy()
        except Exception:
            log.exception("camera %s destroy() failed", self.spec.id)


# ---------- low-level spawn (CARLA) ---------------------------------------


def _make_transform_xyz(
    x: float, y: float, z: float, pitch: float, yaw: float, roll: float
) -> "carla.Transform":
    import carla

    return carla.Transform(
        carla.Location(x=x, y=y, z=z),
        carla.Rotation(pitch=pitch, yaw=yaw, roll=roll),
    )


def _make_blueprint(world: "carla.World", spec: CameraSpec) -> "carla.ActorBlueprint":
    bp = world.get_blueprint_library().find(spec.blueprint)
    bp.set_attribute("image_size_x", str(spec.width))
    bp.set_attribute("image_size_y", str(spec.height))
    bp.set_attribute("fov", str(spec.fov))
    bp.set_attribute("sensor_tick", str(round(1.0 / max(1, spec.fps), 4)))
    return bp


def spawn_camera(
    world: "carla.World",
    spec: CameraSpec,
    queue: FrameQueue,
    attach_to: "carla.Actor | None" = None,
    *,
    record_dir: Path | None = None,
) -> SpawnedCamera:
    """Spawn a CARLA RGB camera and hook its listener to `queue`.

    The transform passed to spawn_actor is treated as:
    - WORLD pose when `attach_to is None`
    - actor-LOCAL offset when `attach_to is not None` (CARLA combines).
    """
    bp = _make_blueprint(world, spec)
    transform = _make_transform_xyz(
        spec.x, spec.y, spec.z, spec.pitch, spec.yaw, spec.roll
    )
    actor = world.spawn_actor(bp, transform, attach_to=attach_to)
    width, height = spec.width, spec.height
    if record_dir is not None:
        record_dir.mkdir(parents=True, exist_ok=True)

    def _on_image(image: "carla.Image") -> None:
        try:
            if record_dir is not None:
                image.save_to_disk(str(record_dir / f"{image.frame:08d}"))
            arr = np.frombuffer(image.raw_data, dtype=np.uint8)
            arr = arr.reshape((height, width, 4))
            rgb = arr[:, :, :3][:, :, ::-1].copy()
            queue.set_latest(rgb)
        except Exception:
            log.exception("camera %s listener crashed on frame", spec.id)

    actor.listen(_on_image)
    log.info(
        "camera %s spawned: mode=%s blueprint=%s res=%dx%d fov=%.1f fps=%d attach=%s",
        spec.id, spec.mode, spec.blueprint, spec.width, spec.height,
        spec.fov, spec.fps, getattr(attach_to, "id", None),
    )
    return SpawnedCamera(spec=spec, actor=actor, queue=queue)


# Spawner signature for dependency injection in tests.
SpawnerFn = Callable[
    ["carla.World", CameraSpec, FrameQueue, "carla.Actor | None"], SpawnedCamera
]


# ---------- bindings ------------------------------------------------------


@dataclass(slots=True)
class CameraBinding:
    """A spec + the entity id (if any) it currently targets.

    Mutable: `attach_entity_id` is what `rebind` swaps. The actual CARLA actor
    is resolved fresh against the Fleet each spawn (so respawn after a
    destroy+rebuild picks up the new actor id automatically).
    """

    spec: CameraSpec

    @property
    def channel_id(self) -> str:
        return self.spec.id

    @property
    def mode(self) -> CameraMode:
        return self.spec.mode

    @property
    def attach_entity_id(self) -> str | None:
        return self.spec.attach_entity_id


def _spawn_for_mode(
    binding: CameraBinding,
    world: "carla.World",
    fleet: Fleet,
    queue: FrameQueue,
    spawner: SpawnerFn,
) -> SpawnedCamera:
    spec = binding.spec
    if spec.mode == "world_pose":
        return spawner(world, spec, queue, None)
    if spec.mode == "attached_to_actor":
        if spec.attach_entity_id is None:
            raise ValueError(
                f"camera {spec.id}: attached_to_actor requires attach_entity_id"
            )
        m = fleet.get(spec.attach_entity_id)
        if not isinstance(m, CarlaActorMember):
            raise ValueError(
                f"camera {spec.id}: target {spec.attach_entity_id!r} is not a "
                f"CarlaActorMember (got {type(m).__name__ if m else 'None'})"
            )
        return spawner(world, spec, queue, m.actor)
    if spec.mode == "follows_virtual":
        if spec.attach_entity_id is None:
            raise ValueError(
                f"camera {spec.id}: follows_virtual requires attach_entity_id"
            )
        m = fleet.get(spec.attach_entity_id)
        if not isinstance(m, VirtualMember):
            raise ValueError(
                f"camera {spec.id}: target {spec.attach_entity_id!r} is not a "
                f"VirtualMember (got {type(m).__name__ if m else 'None'})"
            )
        # Spawn at the current virtual pose + offset; tick post will keep it
        # tracking afterwards.
        initial_transform_spec = _virtual_follow_spec(spec, m.pose())
        return spawner(world, initial_transform_spec, queue, None)
    raise ValueError(f"unknown camera mode: {spec.mode!r}")


def _virtual_follow_spec(spec: CameraSpec, target_pose: Pose) -> CameraSpec:
    """Compose target's world pose with the camera's offset (world-frame).

    Offset is interpreted in world frame for M4 — sufficient for downward-
    looking aerial cameras. M5 may extend to body-frame if a UAV yaws.
    """
    return replace(
        spec,
        x=target_pose.x + spec.x,
        y=target_pose.y + spec.y,
        z=target_pose.z + spec.z,
        pitch=spec.pitch,
        yaw=target_pose.yaw + spec.yaw,
        roll=spec.roll,
    )


# ---------- manager ------------------------------------------------------


class CameraManager:
    """Owns FrameQueues + bindings + spawned cameras.

    `bind()` declares a channel; `spawn_all()` spawns; `rebind()` swaps target
    in place (destroy + respawn with same FrameQueue); `update_followers()`
    is the per-tick hook for follows_virtual cameras.
    """

    def __init__(
        self,
        spawner: SpawnerFn = spawn_camera,
        *,
        record_base: Path | str | None = None,
    ) -> None:
        record_path = Path(record_base) if record_base is not None else None
        if record_path is not None:
            record_path.mkdir(parents=True, exist_ok=True)

            def _recording_spawner(
                world: "carla.World",
                spec: CameraSpec,
                queue: FrameQueue,
                attach_to: "carla.Actor | None" = None,
            ) -> SpawnedCamera:
                return spawner(
                    world,
                    spec,
                    queue,
                    attach_to,
                    record_dir=record_path / spec.id,
                )

            self._spawner = _recording_spawner
            log.info("camera PNG recording enabled under %s", record_path)
        else:
            self._spawner = spawner
        self._record_base = record_path
        self._bindings: dict[str, CameraBinding] = {}
        self._cameras: dict[str, SpawnedCamera] = {}
        self._queues: dict[str, FrameQueue] = {}
        self._lock = RLock()

    # ---- read-only accessors (for /healthz and tests) ----------------

    @property
    def queues(self) -> dict[str, FrameQueue]:
        return self._queues

    @property
    def cameras(self) -> dict[str, SpawnedCamera]:
        return self._cameras

    @property
    def bindings(self) -> dict[str, CameraBinding]:
        return self._bindings

    def queue_for(self, channel_id: str) -> FrameQueue | None:
        return self._queues.get(channel_id)

    def get_binding(self, channel_id: str) -> CameraBinding | None:
        return self._bindings.get(channel_id)

    # ---- queue creation (used by main before signaling routes register) -

    def get_or_create_queue(self, channel_id: str) -> FrameQueue:
        with self._lock:
            q = self._queues.get(channel_id)
            if q is None:
                q = FrameQueue(name=channel_id)
                self._queues[channel_id] = q
            return q

    # ---- binding lifecycle -------------------------------------------

    def bind(self, binding: CameraBinding) -> None:
        """Register/update the binding for a channel. Doesn't spawn yet."""
        with self._lock:
            self._bindings[binding.channel_id] = binding
            # Make sure the queue exists so signaling can find it before spawn.
            self.get_or_create_queue(binding.channel_id)

    def spawn_all(self, world: "carla.World", fleet: Fleet) -> None:
        """Spawn every bound channel that has a resolvable target.

        Channels whose mode requires an `attach_entity_id` but where it's None
        are SKIPPED (M5 scenario `setup_bindings` fills them in and re-spawns
        via `rebind`). Per-channel exceptions are isolated so one bad channel
        doesn't block the others.
        """
        with self._lock:
            channels = list(self._bindings.values())
        for binding in channels:
            spec = binding.spec
            if spec.mode in ("attached_to_actor", "follows_virtual") and (
                spec.attach_entity_id is None
            ):
                log.info(
                    "camera %s: unspawned (mode=%s, no attach_entity_id yet)",
                    binding.channel_id, spec.mode,
                )
                continue
            try:
                self._spawn_one(world, fleet, binding)
            except Exception:
                log.exception("camera %s spawn_all failed", binding.channel_id)

    def _spawn_one(
        self, world: "carla.World", fleet: Fleet, binding: CameraBinding
    ) -> SpawnedCamera:
        queue = self.get_or_create_queue(binding.channel_id)
        cam = _spawn_for_mode(binding, world, fleet, queue, self._spawner)
        # Replace existing camera if any (caller is responsible for detaching
        # the previous one — rebind() does this; spawn_all assumes empty).
        with self._lock:
            self._cameras[binding.channel_id] = cam
        return cam

    # ---- rebind ------------------------------------------------------

    def rebind(
        self,
        channel_id: str,
        new_entity_id: str | None,
        *,
        world: "carla.World",
        fleet: Fleet,
    ) -> SpawnedCamera | None:
        """Swap the channel's target entity. For attached_to_actor and
        follows_virtual: destroy old sensor, spawn new (same FrameQueue).
        For world_pose: just nothing (rebind is a no-op; use spec edit).

        Returns the new SpawnedCamera, or None if the binding doesn't exist.
        """
        with self._lock:
            binding = self._bindings.get(channel_id)
        if binding is None:
            log.warning("rebind: no binding for channel %s", channel_id)
            return None
        if binding.mode == "world_pose":
            log.info("rebind: world_pose channel %s ignored (use spec edit)", channel_id)
            return self._cameras.get(channel_id)

        # Update the binding's target. Spec is frozen-by-convention, so we
        # rebuild with new attach_entity_id.
        new_spec = replace(binding.spec, attach_entity_id=new_entity_id)
        new_binding = CameraBinding(spec=new_spec)
        with self._lock:
            self._bindings[channel_id] = new_binding
            old_cam = self._cameras.pop(channel_id, None)

        if old_cam is not None:
            try:
                old_cam.detach()
            except Exception:
                log.exception("rebind: detach of old %s sensor failed", channel_id)

        # Unbinding (new_entity_id is None): leave the channel without a camera.
        # Teardown uses this path.
        if new_entity_id is None:
            log.info(
                "camera %s unbound (was %s)", channel_id, binding.attach_entity_id
            )
            return None

        try:
            new_cam = self._spawn_one(world, fleet, new_binding)
            log.info(
                "camera %s rebound: %s → %s",
                channel_id, binding.attach_entity_id, new_entity_id,
            )
            return new_cam
        except Exception:
            log.exception("rebind: spawn new sensor for %s failed", channel_id)
            return None

    # ---- per-tick: keep follows_virtual cameras tracking -------------

    def update_followers(self, fleet: Fleet) -> None:
        """Called from the tick thread post-tick.

        For each `follows_virtual` camera, look up its BINDING (which holds the
        immutable offset spec — not the spawned camera's composed pose), fetch
        the bound VirtualMember's current pose, compose, and `set_transform`.
        """
        import carla

        with self._lock:
            items = list(self._cameras.items())
        for channel_id, cam in items:
            binding = self._bindings.get(channel_id)
            if binding is None:
                continue
            offset_spec = binding.spec
            if offset_spec.mode != "follows_virtual" or offset_spec.attach_entity_id is None:
                continue
            m = fleet.get(offset_spec.attach_entity_id)
            if not isinstance(m, VirtualMember):
                continue
            target = m.pose()
            composed = _virtual_follow_spec(offset_spec, target)
            tf = carla.Transform(
                carla.Location(x=composed.x, y=composed.y, z=composed.z),
                carla.Rotation(
                    pitch=composed.pitch, yaw=composed.yaw, roll=composed.roll
                ),
            )
            cam.set_transform(tf)

    # ---- teardown ----------------------------------------------------

    def detach_all(self) -> None:
        with self._lock:
            cams = list(self._cameras.items())
            self._cameras.clear()
        for cid, cam in cams:
            try:
                cam.detach()
            except Exception:  # pragma: no cover
                log.exception("detach failed for %s", cid)


__all__ = [
    "CameraMode",
    "CameraSpec",
    "CameraBinding",
    "SpawnedCamera",
    "spawn_camera",
    "SpawnerFn",
    "CameraManager",
]
