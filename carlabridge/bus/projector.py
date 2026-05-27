"""Projections — pure functions from WorldSnapshot to per-consumer payloads.

- `for_frontend(snap, focus)`: spec §7.1.2 — single-focus {uav, ugv, city}.
  Partial — only fields we have real data for are included; the frontend store
  merges with `Partial<>` so omitted fields keep their previous value.

- `for_agent(snap)`: spec §7.2 — full snapshot, no filtering (D-D1).
"""

from __future__ import annotations

import time
from threading import RLock

from carlabridge.core.snapshot import UavState, VehicleState, WorldSnapshot


class FocusBinding:
    """Which entity id is bound to each frontend slot.

    Updated by scenario `setup_bindings`. Read by `for_frontend` to pick the
    single UAV/UGV to project. Thread-safe — written from sim thread, read
    from async loop.
    """

    __slots__ = ("_uav", "_ugv", "_lock")

    def __init__(self, uav: str | None = None, ugv: str | None = None) -> None:
        self._uav = uav
        self._ugv = ugv
        self._lock = RLock()

    @property
    def uav(self) -> str | None:
        with self._lock:
            return self._uav

    @property
    def ugv(self) -> str | None:
        with self._lock:
            return self._ugv

    def set_uav(self, entity_id: str | None) -> None:
        with self._lock:
            self._uav = entity_id

    def set_ugv(self, entity_id: str | None) -> None:
        with self._lock:
            self._ugv = entity_id

    def snapshot(self) -> tuple[str | None, str | None]:
        with self._lock:
            return self._uav, self._ugv


# ---------- projection helpers --------------------------------------------


def _find_uav(snap: WorldSnapshot, uav_id: str | None) -> UavState | None:
    if uav_id is None:
        return snap.uavs[0] if snap.uavs else None
    for u in snap.uavs:
        if u.id == uav_id:
            return u
    return None


def _find_ugv(snap: WorldSnapshot, ugv_id: str | None) -> VehicleState | None:
    candidates = [v for v in snap.vehicles if v.role == "dispatchable"]
    if ugv_id is None:
        return candidates[0] if candidates else None
    for v in snap.vehicles:
        if v.id == ugv_id:
            return v
    return candidates[0] if candidates else None


def _uav_payload(u: UavState) -> dict:
    # spec §8 UavTelemetry — `gps` mapping is a placeholder until M5 wires
    # `world.get_map().get_geo_location()`. Pose.x/y are meters but the
    # frontend just renders the numbers — exact lat/lng comes later.
    return {
        "id": u.id,
        "altitude": round(u.altitude, 2),
        "speed": round(u.speed, 2),
        "heading": round(u.heading, 1),
        "battery": round(u.battery, 1),
        "gps": {"lat": round(u.pose[0], 5), "lng": round(u.pose[1], 5)},
    }


def _ugv_payload(v: VehicleState) -> dict:
    # spec §8 UgvTelemetry — `road` / `obstacle` / `link` are sentinel for M2
    # (require map waypoint lookup + perception, both later milestones).
    payload: dict = {
        "id": v.id,
        "speed": round(v.speed, 2),
        "heading": round(v.heading, 1),
        "obstacle": "safe",
    }
    if v.battery is not None:
        payload["battery"] = round(v.battery, 1)
    return payload


def _city_payload(snap: WorldSnapshot) -> dict:
    # spec §8 CityMetrics. `pedestrians/aqi/alerts` are placeholders for M2;
    # real walker/AQI/alert wiring lives in M5+.
    total_vehicles = len(snap.vehicles)
    # Treat "congested" as: ≥30% of traffic lights stuck on red.
    red = sum(1 for t in snap.traffic_lights if t.phase == "red")
    state: str = "congested" if snap.traffic_lights and red / max(1, len(snap.traffic_lights)) >= 0.3 else "normal"
    return {
        "vehicles": total_vehicles,
        "pedestrians": 0,
        "intersections": state,
        "aqi": 35,
        "alerts": 0,
    }


# ---------- public projectors --------------------------------------------


def for_frontend(snap: WorldSnapshot, focus: FocusBinding) -> dict:
    """Project to `state_update` payload `{timestamp, uav?, ugv?, city?}`."""
    uav_id, ugv_id = focus.snapshot()
    out: dict = {"timestamp": time.time(), "city": _city_payload(snap)}
    uav = _find_uav(snap, uav_id)
    if uav is not None:
        out["uav"] = _uav_payload(uav)
    ugv = _find_ugv(snap, ugv_id)
    if ugv is not None:
        out["ugv"] = _ugv_payload(ugv)
    return out


def for_agent(snap: WorldSnapshot) -> dict:
    """Project to `state_snapshot` — full, no filtering (spec §7.2 + D1)."""
    return snap.to_dict()


__all__ = ["FocusBinding", "for_frontend", "for_agent"]
