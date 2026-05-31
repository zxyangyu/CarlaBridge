"""Smoke tests for the config layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from carlabridge.config import (
    DEFAULT_CONFIG_PATH,
    Settings,
    load_settings,
)


def test_default_config_loads():
    """`config/default.toml` parses without error and surfaces structurally
    sound values.

    Numeric knobs like ``fixed_delta_seconds`` and ``broadcast.state_hz`` are
    dev-tunable in the toml; this test asserts they are sane positive numbers
    rather than pinning a specific value (so toml retuning doesn't churn the
    test). Stable identifiers (map name, port, scenario) keep exact equality.
    """
    cfg = load_settings()
    assert cfg.carla.map == "sandbox-v19"
    assert isinstance(cfg.carla.fixed_delta_seconds, float)
    assert cfg.carla.fixed_delta_seconds > 0
    assert cfg.server.port == 5000
    assert isinstance(cfg.broadcast.state_hz, float)
    assert cfg.broadcast.state_hz > 0
    assert cfg.scenario.default == "s1_fire"


def test_agent_cfg_removed():
    """Refactor v0.3 — `[agent]` section + AgentCfg are gone."""
    cfg = load_settings()
    assert not hasattr(cfg, "agent")


def test_default_config_path_exists():
    assert DEFAULT_CONFIG_PATH.exists()


def test_env_var_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CARLABRIDGE_CARLA__HOST", "10.0.0.7")
    monkeypatch.setenv("CARLABRIDGE_SERVER__PORT", "6001")
    cfg = load_settings()
    assert cfg.carla.host == "10.0.0.7"
    assert cfg.server.port == 6001


def test_extra_config_overlay(tmp_path: Path):
    overlay = tmp_path / "overlay.toml"
    overlay.write_text(
        "[carla]\nmap = \"Town03\"\n[broadcast]\nstate_hz = 20\n",
        encoding="utf-8",
    )
    cfg = load_settings(extra_config=overlay)
    assert cfg.carla.map == "Town03"
    assert cfg.broadcast.state_hz == 20.0
    # Untouched fields keep defaults.
    assert cfg.server.port == 5000


def test_settings_construct_from_dict():
    # Direct construct (without TOML) — sanity for pydantic defaults.
    s = Settings()
    assert isinstance(s.carla.host, str)


def test_default_camera_resolutions():
    cfg = load_settings()
    assert cfg.video.channel_resolution("city") == (848, 800)
    assert cfg.video.channel_resolution("aerial") == (424, 400)
    assert cfg.video.channel_resolution("ground") == (424, 400)
    assert cfg.video.channel_fps("city") == 25


def test_default_fire_markers():
    cfg = load_settings()
    assert len(cfg.scenario.fire_markers) == 1
    marker = cfg.scenario.fire_markers[0]
    assert marker.id == "fire-001"
    assert marker.x == pytest.approx(250)
    assert marker.y == pytest.approx(-23)
    assert marker.z == pytest.approx(0.5)


def test_fire_markers_overlay(tmp_path: Path):
    overlay = tmp_path / "overlay.toml"
    overlay.write_text(
        '[[scenario.fire_markers]]\nid = "fire-002"\nx = 10.0\ny = 20.0\nz = 1.0\n',
        encoding="utf-8",
    )
    cfg = load_settings(extra_config=overlay)
    assert len(cfg.scenario.fire_markers) == 1
    assert cfg.scenario.fire_markers[0].id == "fire-002"
    assert cfg.scenario.fire_markers[0].x == pytest.approx(10.0)


def test_default_spawn_poses():
    cfg = load_settings()
    vehicle = cfg.scenario.vehicle_spawn
    uav = cfg.scenario.uav_spawn
    assert vehicle is not None
    assert uav is not None
    assert vehicle.x == pytest.approx(214.33909606933594)
    assert vehicle.y == pytest.approx(-43.19072341918945)
    assert uav.z == pytest.approx(10.5)


def test_spawn_pose_overlay(tmp_path: Path):
    overlay = tmp_path / "overlay.toml"
    overlay.write_text(
        "[scenario.vehicle_spawn]\nx = 1.0\ny = 2.0\nz = 0.5\nyaw = 90.0\n"
        "[scenario.uav_spawn]\nx = 1.0\ny = 2.0\nz = 20.0\nyaw = 90.0\n",
        encoding="utf-8",
    )
    cfg = load_settings(extra_config=overlay)
    assert cfg.scenario.vehicle_spawn is not None
    assert cfg.scenario.uav_spawn is not None
    assert cfg.scenario.vehicle_spawn.x == pytest.approx(1.0)
    assert cfg.scenario.uav_spawn.z == pytest.approx(20.0)


def test_default_city_overview_pose():
    cfg = load_settings()
    pose = cfg.camera.city
    assert pose.x == pytest.approx(243.323104858398)
    assert pose.y == pytest.approx(-50.1084709167481)
    assert pose.z == pytest.approx(200.0)
    assert pose.pitch == pytest.approx(-90.0)
    assert pose.fov == pytest.approx(90.0)


def test_city_overview_pose_overlay(tmp_path: Path):
    overlay = tmp_path / "overlay.toml"
    overlay.write_text(
        "[camera.city]\nx = 100.0\ny = -20.0\nz = 150.0\n",
        encoding="utf-8",
    )
    cfg = load_settings(extra_config=overlay)
    assert cfg.camera.city.x == pytest.approx(100.0)
    assert cfg.camera.city.y == pytest.approx(-20.0)
    assert cfg.camera.city.z == pytest.approx(150.0)
    assert cfg.camera.city.pitch == pytest.approx(-90.0)


def test_camera_resolution_overlay(tmp_path: Path):
    overlay = tmp_path / "overlay.toml"
    overlay.write_text(
        "[video.city]\nresolution = [1920, 1080]\n"
        "[video.ground]\nfps = 30\n",
        encoding="utf-8",
    )
    cfg = load_settings(extra_config=overlay)
    assert cfg.video.channel_resolution("city") == (1920, 1080)
    assert cfg.video.channel_resolution("aerial") == (424, 400)
    assert cfg.video.channel_fps("ground") == 30
