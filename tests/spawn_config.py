"""Shared spawn poses for tests that run S1FireScenario.setup()."""

from __future__ import annotations

from carlabridge.config import EntitySpawnCfg, FireMarkerCfg, Settings


def make_spawn_settings(
    *,
    vehicle: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0),
    uav: tuple[float, float, float, float] = (0.0, 0.0, 60.0, 0.0),
    fire_markers: list[FireMarkerCfg] | None = None,
) -> Settings:
    vx, vy, vz, vyaw = vehicle
    ux, uy, uz, uyaw = uav
    settings = Settings()
    settings.scenario.vehicle_spawn = EntitySpawnCfg(
        x=vx, y=vy, z=vz, yaw=vyaw,
    )
    settings.scenario.uav_spawn = EntitySpawnCfg(
        x=ux, y=uy, z=uz, yaw=uyaw,
    )
    settings.scenario.fire_markers = fire_markers or [
        FireMarkerCfg(id="fire-001", x=30.0, y=0.0, z=0.0),
        FireMarkerCfg(id="fire-002", x=40.0, y=0.0, z=0.0),
    ]
    return settings
