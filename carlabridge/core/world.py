"""CARLA world facade.

Owns CARLA connection, sync-mode lifecycle, and map check. All `carla.Client`
calls go through here; nothing else in the bridge should `import carla` for
control purposes (Snapshot/sensor code is read-only).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    import carla

log = logging.getLogger(__name__)


class BridgeFatal(RuntimeError):
    """Unrecoverable error; main loop should exit with non-zero status."""


class World:
    """CARLA connection + sync-mode lifecycle.

    Usage:
        w = World.connect(host, port, timeout_s)
        w.save_original_settings()
        w.ensure_map("Town10HD_Opt")
        w.switch_to_sync(0.0333)
        try:
            ...
        finally:
            w.restore_original_settings()
            w.disconnect()
    """

    def __init__(self, client: "carla.Client") -> None:
        self._client = client
        self._world: "carla.World" = client.get_world()
        self._original_settings: Optional["carla.WorldSettings"] = None
        self._sync_active = False

    # ---- construction --------------------------------------------------

    @classmethod
    def connect(cls, host: str, port: int, timeout_s: float) -> "World":
        try:
            import carla  # local import — keeps unit tests carla-free
        except ImportError as e:
            raise BridgeFatal(
                "carla python package not installed in this env"
            ) from e
        try:
            client = carla.Client(host, port)
            client.set_timeout(float(timeout_s))
            # Force an RPC roundtrip to fail fast if the server is down.
            version = client.get_server_version()
        except Exception as e:
            raise BridgeFatal(
                f"cannot reach CARLA at {host}:{port} (timeout={timeout_s}s) — "
                f"is CarlaUE4 running? underlying error: {e}"
            ) from e
        log.info("connected to CARLA server %s at %s:%d", version, host, port)
        return cls(client)

    # ---- accessors -----------------------------------------------------

    @property
    def carla_world(self) -> "carla.World":
        return self._world

    @property
    def client(self) -> "carla.Client":
        return self._client

    def current_map_name(self) -> str:
        # `Carla/Maps/Town10HD_Opt` → `Town10HD_Opt`
        full = self._world.get_map().name
        return full.rsplit("/", 1)[-1]

    # ---- map / settings ------------------------------------------------

    def ensure_map(self, name: str, *, force_reload: bool = False) -> None:
        """Load `name` if current map differs (or force_reload). Blocks until ready."""
        current = self.current_map_name()
        if not force_reload and current == name:
            log.info("map already loaded: %s", current)
            return
        log.info("loading map %s (current=%s)…", name, current)
        try:
            self._world = self._client.load_world(name)
        except Exception as e:
            raise BridgeFatal(f"failed to load map {name!r}: {e}") from e
        # load_world returns once the new map is active; settle a tick.
        time.sleep(0.5)
        log.info("map loaded: %s", self.current_map_name())

    def save_original_settings(self) -> None:
        self._original_settings = self._world.get_settings()
        log.debug(
            "saved original settings: sync=%s delta=%s",
            self._original_settings.synchronous_mode,
            self._original_settings.fixed_delta_seconds,
        )

    def switch_to_sync(self, fixed_delta_seconds: float) -> None:
        if self._original_settings is None:
            raise BridgeFatal("call save_original_settings() before switch_to_sync()")
        settings = self._world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = float(fixed_delta_seconds)
        # M6 diag: substepping was causing 30s RPC timeouts inside BasicAgent.
        # Disabling it makes physics single-step per tick — fine for this demo.
        settings.substepping = False
        try:
            self._world.apply_settings(settings)
        except Exception as e:
            raise BridgeFatal(f"failed to enter sync mode: {e}") from e
        self._sync_active = True
        log.info("CARLA → sync mode, fixed_delta=%.4fs", fixed_delta_seconds)

    def restore_original_settings(self) -> None:
        if self._original_settings is None:
            log.debug("no original settings saved; skipping restore")
            return
        try:
            self._world.apply_settings(self._original_settings)
            log.info("CARLA → original (async) settings restored")
        except Exception as e:
            # Best-effort during shutdown.
            log.warning("failed to restore original settings: %s", e)
        finally:
            self._sync_active = False

    def disconnect(self) -> None:
        # carla.Client has no explicit close; let Python GC release the socket.
        # We keep a method for symmetric lifecycle in main.py.
        log.info("CARLA client released")

    # ---- thin pass-throughs --------------------------------------------

    def tick(self) -> int:
        """Advance one sim step. Returns the new frame number.

        Raises BridgeFatal on RPC errors so the tick loop can decide policy.
        """
        try:
            return self._world.tick()
        except Exception as e:
            raise BridgeFatal(f"world.tick() failed: {e}") from e

    def get_snapshot(self) -> "carla.WorldSnapshot":
        return self._world.get_snapshot()
