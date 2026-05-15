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
    assert cfg.carla.map == "Town10HD_Opt"
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
