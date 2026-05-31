"""Configuration model. TOML-first, env-var overrides.

Layered load order (later wins):
    1. config/default.toml  (always loaded)
    2. config/local.toml    (optional, gitignored)
    3. file passed via --config CLI arg
    4. env vars with prefix CARLABRIDGE_ (double-underscore = nesting)
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "default.toml"
LOCAL_CONFIG_PATH = REPO_ROOT / "config" / "local.toml"


class CarlaCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 2000
    timeout_s: float = 10.0
    fixed_delta_seconds: float = 0.0333
    map: str = "Town10HD_Opt"


class ServerCfg(BaseModel):
    host: str = "0.0.0.0"
    port: int = 5000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])


class BroadcastCfg(BaseModel):
    state_hz: float = 10.0
    metrics_hz: float = 1.0


class CameraChannelCfg(BaseModel):
    resolution: tuple[int, int] | None = None
    fps: int | None = None


class VideoCfg(BaseModel):
    default_fps: int = 25
    default_resolution: tuple[int, int] = (1280, 720)
    frame_queue_drop_log_interval_s: float = 5.0
    city: CameraChannelCfg = Field(default_factory=CameraChannelCfg)
    aerial: CameraChannelCfg = Field(default_factory=CameraChannelCfg)
    ground: CameraChannelCfg = Field(default_factory=CameraChannelCfg)

    def channel_resolution(
        self, channel: Literal["city", "aerial", "ground"]
    ) -> tuple[int, int]:
        cfg = getattr(self, channel)
        return cfg.resolution if cfg.resolution is not None else self.default_resolution

    def channel_fps(self, channel: Literal["city", "aerial", "ground"]) -> int:
        cfg = getattr(self, channel)
        return cfg.fps if cfg.fps is not None else self.default_fps


class ScenarioCfg(BaseModel):
    default: str = "s1_fire"


class LoggingCfg(BaseModel):
    level: str = "INFO"
    event_log_buffer: int = 1000


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CARLABRIDGE_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    carla: CarlaCfg = Field(default_factory=CarlaCfg)
    server: ServerCfg = Field(default_factory=ServerCfg)
    broadcast: BroadcastCfg = Field(default_factory=BroadcastCfg)
    video: VideoCfg = Field(default_factory=VideoCfg)
    scenario: ScenarioCfg = Field(default_factory=ScenarioCfg)
    logging: LoggingCfg = Field(default_factory=LoggingCfg)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Make env vars win over kwargs passed to Settings(...) (which carry TOML values).
        # Final precedence: env > dotenv > file_secret > init/TOML > field defaults.
        return (env_settings, dotenv_settings, file_secret_settings, init_settings)


def _read_toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def load_settings(extra_config: Path | None = None) -> Settings:
    """Merge TOML layers, then let pydantic-settings apply env overrides."""
    merged: dict = {}
    for path in (DEFAULT_CONFIG_PATH, LOCAL_CONFIG_PATH, extra_config):
        if path is None:
            continue
        if not path.exists():
            if path is DEFAULT_CONFIG_PATH:
                raise FileNotFoundError(f"required config missing: {path}")
            continue
        _deep_merge(merged, _read_toml(path))
    return Settings(**merged)


def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
