#!/usr/bin/env python3
"""Interactive CARLA pose designer — city camera + vehicle spawn positions.

Requires a running CARLA server (same version as pyproject.toml, default 0.9.16).
Uses config/default.toml (+ optional config/local.toml) for host/port/map.

Usage (from repo root, venv active):
    python scripts/design_poses.py
    python scripts/design_poses.py --spawn-index 5
    python scripts/design_poses.py --out config/design_poses.json

Controls
--------
  WASD / arrows     Move horizontally (relative to view yaw)
  Q / E             Move down / up
  Mouse (hold RMB)  Look around (pitch / yaw)
  J / L             Rotate yaw (keyboard)
  I / K             Pitch up / down (keyboard)
  [ / ]             Previous / next map spawn point (teleport)
  C                 Mark **city overview camera** (current view pose)
  G                 Mark **vehicle spawn** (current position, snapped to ground)
  F                 Mark **fire / incident** marker at ground under crosshair
  P                 Spawn preview firetruck at marked vehicle spawn
  X                 Destroy preview actors
  Enter             Print export snippet to stdout
  O                 Write export JSON (+ TOML snippet) to --out path
  H                 Toggle help overlay
  Esc               Quit (preview actors destroyed)
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pygame

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from carlabridge.config import load_settings  # noqa: E402

log = logging.getLogger("design_poses")

PREVIEW_BP = "vehicle.carlamotors.firetruck"
UAV_ALTITUDE = 10.0


@dataclass
class Pose6:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0
    roll: float = 0.0
    fov: float = 90.0


@dataclass
class DesignExport:
    map_name: str = ""
    saved_at: str = ""
    city_camera: Pose6 | None = None
    vehicle_spawn: Pose6 | None = None
    spawn_point_index: int | None = None
    fire_markers: list[Pose6] = field(default_factory=list)
    uav_origins: list[Pose6] = field(default_factory=list)

    def recompute_uav_origins(self) -> None:
        if self.vehicle_spawn is None:
            self.uav_origins = []
            return
        s = self.vehicle_spawn
        self.uav_origins = [
            Pose6(x=s.x, y=s.y, z=s.z + UAV_ALTITUDE, yaw=s.yaw),
        ]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=None, help="CARLA host (default: from config)")
    p.add_argument("--port", type=int, default=None, help="CARLA port (default: from config)")
    p.add_argument("--map", default=None, help="Load this map if different from current")
    p.add_argument("--spawn-index", type=int, default=None, help="Start at spawn point index")
    p.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "config" / "design_poses.json",
        help="Export path for JSON + TOML snippet (default: config/design_poses.json)",
    )
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fov", type=float, default=90.0)
    p.add_argument(
        "--no-preview-camera",
        action="store_true",
        help="Do not spawn a CARLA RGB sensor; safer on large-map/offscreen builds that crash in sensor teardown.",
    )
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


def _connect(host: str, port: int, timeout_s: float):
    import carla

    client = carla.Client(host, port)
    client.set_timeout(timeout_s)
    version = client.get_server_version()
    log.info("connected to CARLA %s at %s:%d", version, host, port)
    return client


def _ensure_map(client, map_name: str | None):
    world = client.get_world()
    if not map_name:
        return world
    current = world.get_map().name.rsplit("/", 1)[-1]
    if current != map_name:
        log.info("loading map %s (was %s)", map_name, current)
        world = client.load_world(map_name)
    return world


def _apply_pose_designer_settings(world):
    settings = world.get_settings()
    original = world.get_settings()
    changed = False

    if getattr(settings, "synchronous_mode", False):
        settings.synchronous_mode = False
        settings.fixed_delta_seconds = None
        changed = True
    if hasattr(settings, "spectator_as_ego") and not settings.spectator_as_ego:
        settings.spectator_as_ego = True
        changed = True
    if hasattr(settings, "tile_stream_distance") and settings.tile_stream_distance < 200000.0:
        settings.tile_stream_distance = 200000.0
        changed = True
    if hasattr(settings, "actor_active_distance") and settings.actor_active_distance < 200000.0:
        settings.actor_active_distance = 200000.0
        changed = True

    if changed:
        world.apply_settings(settings)
        log.info("applied pose-designer world settings")
    return original if changed else None


def _restore_world_settings(world, original_settings) -> None:
    if original_settings is None:
        return
    try:
        world.apply_settings(original_settings)
        log.info("restored original world settings")
    except Exception as e:
        log.warning("failed to restore original world settings: %s", e)


def _tf_to_pose6(tf, *, fov: float = 90.0) -> Pose6:
    loc, rot = tf.location, tf.rotation
    return Pose6(
        x=loc.x, y=loc.y, z=loc.z,
        pitch=rot.pitch, yaw=rot.yaw, roll=rot.roll,
        fov=fov,
    )


def _pose6_to_transform(carla, pose: Pose6):
    return carla.Transform(
        carla.Location(x=pose.x, y=pose.y, z=pose.z),
        carla.Rotation(pitch=pose.pitch, yaw=pose.yaw, roll=pose.roll),
    )


def _ground_at(world, x: float, y: float, z_hint: float = 300.0):
    import carla

    start = carla.Location(x=x, y=y, z=z_hint)
    end = carla.Location(x=x, y=y, z=-100.0)
    hit = world.cast_ray(start, end)
    if hit:
        return hit[0].location.z
    return z_hint


def _snap_spawn(world, spectator_tf) -> tuple[Pose6, int | None]:
    """Return pose on ground under spectator; prefer nearest map spawn point."""
    import carla

    spawn_points = world.get_map().get_spawn_points()
    loc = spectator_tf.location
    gz = _ground_at(world, loc.x, loc.y, z_hint=loc.z + 50.0)
    best_idx: int | None = None
    best_dist = float("inf")
    for i, sp in enumerate(spawn_points):
        dx = sp.location.x - loc.x
        dy = sp.location.y - loc.y
        d = math.hypot(dx, dy)
        if d < best_dist:
            best_dist = d
            best_idx = i
    # Within 8 m → use official spawn point (same logic as s1_fire anchor).
    if best_idx is not None and best_dist <= 8.0:
        sp = spawn_points[best_idx]
        return _tf_to_pose6(sp), best_idx
    pose = Pose6(x=loc.x, y=loc.y, z=gz + 0.3, yaw=spectator_tf.rotation.yaw)
    return pose, None


def _forward_yaw(yaw_deg: float) -> tuple[float, float]:
    rad = math.radians(yaw_deg)
    return math.cos(rad), math.sin(rad)


def _move_pose(pose: Pose6, *, forward: float, right: float, up: float) -> Pose6:
    fx, fy = _forward_yaw(pose.yaw)
    rx, ry = -fy, fx
    return Pose6(
        x=pose.x + fx * forward + rx * right,
        y=pose.y + fy * forward + ry * right,
        z=pose.z + up,
        pitch=pose.pitch,
        yaw=pose.yaw,
        roll=pose.roll,
        fov=pose.fov,
    )


def _carla_image_to_surface(image) -> pygame.Surface:
    arr = np.frombuffer(image.raw_data, dtype=np.uint8)
    arr = arr.reshape((image.height, image.width, 4))
    rgb = arr[:, :, :3]
    surface = pygame.surfarray.make_surface(rgb.swapaxes(0, 1))
    return surface.convert()


def _export_toml_snippet(exp: DesignExport) -> str:
    lines = ["# Paste into config/local.toml (or default.toml)", ""]
    if exp.city_camera:
        c = exp.city_camera
        lines.extend([
            "[camera.city]",
            f"x = {c.x:.2f}",
            f"y = {c.y:.2f}",
            f"z = {c.z:.2f}",
            f"pitch = {c.pitch:.2f}",
            f"yaw = {c.yaw:.2f}",
            f"roll = {c.roll:.2f}",
            f"fov = {c.fov:.1f}",
            "",
        ])
    if exp.vehicle_spawn:
        s = exp.vehicle_spawn
        lines.extend([
            "[scenario.vehicle_spawn]",
            f"x = {s.x:.2f}",
            f"y = {s.y:.2f}",
            f"z = {s.z:.2f}",
            f"yaw = {s.yaw:.2f}",
            "",
        ])
    if exp.uav_origins:
        u = exp.uav_origins[0]
        lines.extend([
            "[scenario.uav_spawn]",
            f"x = {u.x:.2f}",
            f"y = {u.y:.2f}",
            f"z = {u.z:.2f}",
            f"yaw = {u.yaw:.2f}",
            "",
        ])
    if exp.fire_markers:
        for i, m in enumerate(exp.fire_markers, 1):
            marker_id = f"fire-{i:03d}"
            lines.extend([
                "[[scenario.fire_markers]]",
                f'id = "{marker_id}"',
                f"x = {m.x:.2f}",
                f"y = {m.y:.2f}",
                f"z = {m.z:.2f}",
                "",
            ])
    return "\n".join(lines)


def _write_export(path: Path, exp: DesignExport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "map_name": exp.map_name,
        "saved_at": exp.saved_at,
        "city_camera": asdict(exp.city_camera) if exp.city_camera else None,
        "vehicle_spawn": asdict(exp.vehicle_spawn) if exp.vehicle_spawn else None,
        "spawn_point_index": exp.spawn_point_index,
        "fire_markers": [asdict(m) for m in exp.fire_markers],
        "uav_origins": [asdict(u) for u in exp.uav_origins],
        "constants": {"UAV_ALTITUDE": UAV_ALTITUDE},
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    toml_path = path.with_suffix(".toml.snippet")
    toml_path.write_text(_export_toml_snippet(exp), encoding="utf-8")
    log.info("wrote %s and %s", path, toml_path)


def _print_export(exp: DesignExport) -> None:
    print("\n" + "=" * 60)
    print(_export_toml_snippet(exp))
    print("=" * 60 + "\n")


def _spawn_preview_vehicle(world, pose: Pose6, actors: list) -> str | None:
    bps = world.get_blueprint_library().filter(PREVIEW_BP)
    if not bps:
        return f"blueprint {PREVIEW_BP!r} not found"
    tf = _pose6_to_transform(__import__("carla"), pose)
    actor = world.try_spawn_actor(bps[0], tf)
    if actor is None:
        return "spawn failed (collision?)"
    actors.append(actor)
    return None


def _destroy_actors(actors: list) -> None:
    for a in actors:
        try:
            if hasattr(a, "stop"):
                a.stop()
        except Exception:
            pass
        try:
            a.destroy()
        except Exception:
            pass
    actors.clear()


def _draw_hud(
    screen: pygame.Surface,
    font: pygame.font.Font,
    *,
    view: Pose6,
    export: DesignExport,
    spawn_idx: int,
    spawn_total: int,
    show_help: bool,
    status: str,
) -> None:
    w, h = screen.get_size()
    overlay = pygame.Surface((w, h), pygame.SRCALPHA)
    panel_h = 220 if not show_help else min(h - 40, 520)
    pygame.draw.rect(overlay, (0, 0, 0, 170), (8, 8, 420, panel_h))
    screen.blit(overlay, (0, 0))

    lines = [
        f"View  x={view.x:8.1f}  y={view.y:8.1f}  z={view.z:6.1f}",
        f"      pitch={view.pitch:6.1f}  yaw={view.yaw:6.1f}  roll={view.roll:5.1f}",
        f"Spawn point [{spawn_idx + 1}/{spawn_total}]  ( [ ] to cycle )",
        "",
        f"City camera:    {'SET' if export.city_camera else '—'}  [C]",
        f"Vehicle spawn:  {'SET' if export.vehicle_spawn else '—'}  [G]  preview [P]",
        f"Fire markers:   {len(export.fire_markers)}  [F]",
        "",
        status,
    ]
    if show_help:
        lines.extend([
            "",
            "Move: WASD/arrows  Q/E up/down",
            "Look: hold RMB+mouse  or  I/K/J/L",
            "Export: Enter (print)  O (write file)  Esc quit",
        ])

    y = 16
    for line in lines:
        surf = font.render(line, True, (240, 240, 240))
        screen.blit(surf, (16, y))
        y += 20


def main() -> int:
    args = _parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    settings = load_settings()
    host = args.host or settings.carla.host
    port = args.port or settings.carla.port
    map_name = args.map or settings.carla.map

    try:
        client = _connect(host, port, settings.carla.timeout_s)
    except Exception as e:
        log.error("cannot connect: %s", e)
        return 1

    import carla

    world = _ensure_map(client, map_name)
    original_settings = _apply_pose_designer_settings(world)
    map_label = world.get_map().name.rsplit("/", 1)[-1]
    spawn_points = world.get_map().get_spawn_points()
    if not spawn_points:
        log.warning("map has no spawn points — vehicle spawn uses free coordinates only")

    spectator = world.get_spectator()
    export = DesignExport(map_name=map_label, saved_at=datetime.now(UTC).isoformat())

    spawn_cursor = 0
    if args.spawn_index is not None:
        spawn_cursor = max(0, min(args.spawn_index, len(spawn_points) - 1))
    if spawn_points:
        spectator.set_transform(spawn_points[spawn_cursor])

    view = _tf_to_pose6(spectator.get_transform(), fov=args.fov)
    preview_actors: list = []
    cam_actor = None
    if not args.no_preview_camera:
        # Preview RGB camera (what you see in the pygame window).
        cam_bp = world.get_blueprint_library().find("sensor.camera.rgb")
        cam_bp.set_attribute("image_size_x", str(args.width))
        cam_bp.set_attribute("image_size_y", str(args.height))
        cam_bp.set_attribute("fov", str(args.fov))
        cam_actor = world.spawn_actor(cam_bp, _pose6_to_transform(carla, view))
        preview_actors.append(cam_actor)

    pygame.init()
    pygame.display.set_caption(f"CarlaBridge pose designer — {map_label}")
    screen = pygame.display.set_mode((args.width, args.height))
    font = pygame.font.SysFont("monospace", 15)
    clock = pygame.time.Clock()

    image_surface: pygame.Surface | None = None
    status_msg = "Ready"
    show_help = True
    mouse_delta = [0, 0]
    rmb_down = False

    def on_image(image):
        nonlocal image_surface
        image_surface = _carla_image_to_surface(image)

    if cam_actor is not None:
        cam_actor.listen(on_image)

    def clear_non_camera_actors() -> None:
        camera_id = cam_actor.id if cam_actor is not None else None
        victims = [a for a in preview_actors if camera_id is None or a.id != camera_id]
        _destroy_actors(victims)
        preview_actors[:] = [a for a in preview_actors if camera_id is not None and a.id == camera_id]

    move_speed = 0.35
    rot_speed = 1.2
    mouse_sens = 0.25

    try:
        running = True
        while running:
            dt = clock.tick(60) / 1000.0
            move_speed_frame = move_speed * (60.0 * dt)

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        running = False
                    elif event.key == pygame.K_h:
                        show_help = not show_help
                    elif event.key == pygame.K_RETURN:
                        export.saved_at = datetime.now(UTC).isoformat()
                        _print_export(export)
                        status_msg = "Printed export to stdout"
                    elif event.key == pygame.K_o:
                        export.saved_at = datetime.now(UTC).isoformat()
                        _write_export(args.out, export)
                        status_msg = f"Wrote {args.out}"
                    elif event.key == pygame.K_c:
                        export.city_camera = Pose6(
                            x=view.x, y=view.y, z=view.z,
                            pitch=view.pitch, yaw=view.yaw, roll=view.roll,
                            fov=args.fov,
                        )
                        status_msg = "Marked city overview camera"
                    elif event.key == pygame.K_g:
                        pose, idx = _snap_spawn(world, _pose6_to_transform(carla, view))
                        export.vehicle_spawn = pose
                        export.spawn_point_index = idx
                        export.recompute_uav_origins()
                        if idx is not None:
                            status_msg = f"Marked vehicle spawn (spawn point #{idx})"
                        else:
                            status_msg = "Marked vehicle spawn (free position)"
                    elif event.key == pygame.K_f:
                        gz = _ground_at(world, view.x, view.y, z_hint=view.z + 50.0)
                        export.fire_markers.append(
                            Pose6(x=view.x, y=view.y, z=gz + 0.5, yaw=view.yaw)
                        )
                        status_msg = f"Fire marker #{len(export.fire_markers)} added"
                    elif event.key == pygame.K_p:
                        if export.vehicle_spawn is None:
                            status_msg = "Mark vehicle spawn [G] first"
                        else:
                            clear_non_camera_actors()
                            err = _spawn_preview_vehicle(world, export.vehicle_spawn, preview_actors)
                            status_msg = "Preview vehicle spawned" if err is None else err
                    elif event.key == pygame.K_x:
                        clear_non_camera_actors()
                        status_msg = "Preview actors cleared"
                    elif event.key == pygame.K_LEFTBRACKET and spawn_points:
                        spawn_cursor = (spawn_cursor - 1) % len(spawn_points)
                        tf = spawn_points[spawn_cursor]
                        spectator.set_transform(tf)
                        view = _tf_to_pose6(tf, fov=args.fov)
                        status_msg = f"Spawn point #{spawn_cursor}"
                    elif event.key == pygame.K_RIGHTBRACKET and spawn_points:
                        spawn_cursor = (spawn_cursor + 1) % len(spawn_points)
                        tf = spawn_points[spawn_cursor]
                        spectator.set_transform(tf)
                        view = _tf_to_pose6(tf, fov=args.fov)
                        status_msg = f"Spawn point #{spawn_cursor}"
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 3:
                    rmb_down = True
                    pygame.event.set_grab(True)
                elif event.type == pygame.MOUSEBUTTONUP and event.button == 3:
                    rmb_down = False
                    pygame.event.set_grab(False)
                elif event.type == pygame.MOUSEMOTION and rmb_down:
                    mouse_delta[0] += event.rel[0]
                    mouse_delta[1] += event.rel[1]

            keys = pygame.key.get_pressed()
            forward = right = up = 0.0
            if keys[pygame.K_w] or keys[pygame.K_UP]:
                forward += move_speed_frame
            if keys[pygame.K_s] or keys[pygame.K_DOWN]:
                forward -= move_speed_frame
            if keys[pygame.K_d] or keys[pygame.K_RIGHT]:
                right += move_speed_frame
            if keys[pygame.K_a] or keys[pygame.K_LEFT]:
                right -= move_speed_frame
            if keys[pygame.K_e]:
                up += move_speed_frame
            if keys[pygame.K_q]:
                up -= move_speed_frame

            if forward or right or up:
                view = _move_pose(view, forward=forward, right=right, up=up)

            if rmb_down and (mouse_delta[0] or mouse_delta[1]):
                view.yaw += mouse_delta[0] * mouse_sens
                view.pitch = max(-89.0, min(89.0, view.pitch - mouse_delta[1] * mouse_sens))
                mouse_delta[0] = mouse_delta[1] = 0
            else:
                if keys[pygame.K_l]:
                    view.yaw += rot_speed
                if keys[pygame.K_j]:
                    view.yaw -= rot_speed
                if keys[pygame.K_i]:
                    view.pitch = min(89.0, view.pitch + rot_speed * 0.5)
                if keys[pygame.K_k]:
                    view.pitch = max(-89.0, view.pitch - rot_speed * 0.5)

            tf = _pose6_to_transform(carla, view)
            spectator.set_transform(tf)
            if cam_actor is not None:
                cam_actor.set_transform(tf)
            world.wait_for_tick(1.0)

            if image_surface is not None:
                screen.blit(image_surface, (0, 0))
            else:
                screen.fill((30, 30, 30))

            _draw_hud(
                screen, font,
                view=view,
                export=export,
                spawn_idx=spawn_cursor,
                spawn_total=max(1, len(spawn_points)),
                show_help=show_help,
                status=status_msg,
            )
            pygame.display.flip()
    finally:
        _destroy_actors(preview_actors)
        _restore_world_settings(world, original_settings)
        pygame.quit()
    log.info("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
