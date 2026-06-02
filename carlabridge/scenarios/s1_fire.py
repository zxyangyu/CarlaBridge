"""S1 — Fire emergency scenario (refactor v0.3).

Bridge is the **execution layer** for this scenario (design §1):

* ``setup()`` spawns one UGV, registers 1 virtual UAV, binds cameras, and
  fills ``fleet.origins`` so ``*_RTL`` commands have somewhere to go back to.
* ``on_command()`` dispatches the 8 commands (design §3.2) through
  ``_accept_command`` (supersede) + private ``_handle_*`` methods.
* ``on_tick_post()`` advances UAV lerp + UGV ``SimpleWaypointFollower``,
  drives patrol indexing, then calls
  :meth:`Scenario._drive_command_lifecycle` to finalise any command.
* ``ignite_fire(...)`` / ``reset()`` are operator entry points
  (HTTP /scenario/fire and /scenario/reset, wired in R6).

The scenario is **time-independent**: it never reacts to ``sim_time`` on its
own. Every state transition is driven by either a command or an HTTP call.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from carlabridge.commands.enum import CommandKind, ParsedCommand, RejectCommand
from carlabridge.core.fleet import CarlaActorMember, Pose, VirtualMember
from carlabridge.core.incident import Incident
from carlabridge.scenarios.base import CompletionResult, Scenario, register_scenario
from carlabridge.scenarios.in_flight import InFlightCommand
from carlabridge.scenarios.waypoint_follower import SimpleWaypointFollower

if TYPE_CHECKING:  # pragma: no cover
    from carlabridge.config import Settings
    import carla

log = logging.getLogger(__name__)


# ---------- tuning constants (Town10HD_Opt) -------------------------------


UGV_BLUEPRINT_CANDIDATES = (
    # "vehicle.carlamotors.firetruck",
    # "vehicle.lincoln.mkz_2017",
    "vehicle.tesla.model3",
)
# If the firetruck cannot fit a spawn point, try another emergency vehicle only.
UGV_BLUEPRINT_FALLBACKS = (
    "vehicle.ford.ambulance",
)

# Static props only — vehicles have physics and collide with the UGV / road traffic.
FIRE_MARKER_BLUEPRINTS = (
    "static.prop.streetbarrier",
    "static.prop.kiosk_01",
    "static.prop.barrel",
)

UAV_ALTITUDE = 10.0          # default offset for design_poses export only

# Refactor v0.3 — config-pinned thresholds (design §7.7)
UAV_ARRIVAL_EPS_M = 0.5            # UAV GOTO/RTL/PATROL waypoint reach radius
EXTINGUISH_RADIUS_M = 8.0          # UGV must be within this of an incident
# Sim seconds after accept before fire actor is destroyed and command completes.
EXTINGUISH_DWELL_S = 3.0
DEFAULT_UAV_RTL_SPEED = 8.0        # m/s when params.cruise_speed missing
DEFAULT_UGV_TARGET_SPEED_KMH = 25.0


# ---------- scenario ------------------------------------------------------


@register_scenario("s1_fire")
class S1FireScenario(Scenario):
    """1 UAV + 1 UGV + 0..N fire incidents; cameras bound to UAV-01 / UGV-01."""

    def __init__(self, *, settings: "Settings | None" = None, **kwargs):
        super().__init__(settings=settings, **kwargs)
        # UGV follower: non-None when a GOTO/RTL is in flight.
        self._ugv_follower: SimpleWaypointFollower | None = None
        # Anchor of the spawn point used by setup; needed for fallbacks.
        self._anchor_world_xyz: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._anchor_yaw: float = 0.0
        # incident_id → fire actor handle.
        self._fire_actors: dict[str, "carla.Actor"] = {}
        # Reset gate — HTTP /scenario/reset flips this around teardown+setup.
        self._resetting: bool = False
        # Unique counter for auto-generated incident ids.
        self._next_incident_seq: int = 0

    # ---- lifecycle ----------------------------------------------------

    def _require_spawn_config(self):
        from carlabridge.config import EntitySpawnCfg

        if self.settings is None:
            raise RuntimeError("S1: settings not provided to scenario")
        vehicle_spawn = self.settings.scenario.vehicle_spawn
        uav_spawn = self.settings.scenario.uav_spawn
        if vehicle_spawn is None or uav_spawn is None:
            raise RuntimeError(
                "S1: scenario.vehicle_spawn and scenario.uav_spawn must be set in config"
            )
        if not isinstance(vehicle_spawn, EntitySpawnCfg) or not isinstance(uav_spawn, EntitySpawnCfg):
            raise RuntimeError("S1: invalid spawn config types")
        return vehicle_spawn, uav_spawn

    def setup(self) -> None:
        carla_world = self.world.carla_world
        vehicle_spawn, uav_spawn = self._require_spawn_config()

        bp_lib = carla_world.get_blueprint_library()
        preferred = list(_filter_existing(bp_lib, UGV_BLUEPRINT_CANDIDATES))
        fallbacks = list(_filter_existing(bp_lib, UGV_BLUEPRINT_FALLBACKS))
        seen_ids: set[str] = set()
        candidates = []
        for bp in preferred + fallbacks:
            if bp.id in seen_ids:
                continue
            seen_ids.add(bp.id)
            candidates.append(bp)
        if not candidates:
            raise RuntimeError("S1: no UGV blueprint available in CARLA library")

        configured_transform = _make_transform(
            x=vehicle_spawn.x,
            y=vehicle_spawn.y,
            z=vehicle_spawn.z,
            yaw=vehicle_spawn.yaw,
        )
        log.info(
            "S1: spawning UGV at configured (%.1f, %.1f, %.1f) yaw=%.1f",
            vehicle_spawn.x, vehicle_spawn.y, vehicle_spawn.z, vehicle_spawn.yaw,
        )

        ugv_actor, anchor_transform, bp_id = _spawn_ugv(
            carla_world, candidates, configured_transform, label="configured pose",
        )
        if ugv_actor is None:
            spawn_points = carla_world.get_map().get_spawn_points()
            if not spawn_points:
                raise RuntimeError(
                    f"S1: no UGV at configured pose "
                    f"({vehicle_spawn.x}, {vehicle_spawn.y}, {vehicle_spawn.z}) "
                    f"and map has no spawn points to fall back to"
                )
            log.warning(
                "S1: configured pose failed — trying %d map spawn point(s)",
                len(spawn_points),
            )
            for sp_tf in spawn_points:
                ugv_actor, anchor_transform, bp_id = _spawn_ugv(
                    carla_world, candidates, sp_tf, label="map spawn point",
                )
                if ugv_actor is not None:
                    sx, sy, sz, syaw = _transform_pose(sp_tf)
                    log.info(
                        "S1: UGV fallback spawn ok at map point (%.1f, %.1f, %.1f) yaw=%.1f",
                        sx, sy, sz, syaw,
                    )
                    break
        if ugv_actor is None or anchor_transform is None or bp_id is None:
            n_sp = len(carla_world.get_map().get_spawn_points())
            raise RuntimeError(
                f"S1: no UGV could be spawned at configured pose "
                f"({vehicle_spawn.x}, {vehicle_spawn.y}, {vehicle_spawn.z}) "
                f"or at any of {n_sp} map spawn point(s)"
            )

        ax, ay, az, anchor_yaw = _transform_pose(anchor_transform)
        self._anchor_world_xyz = (ax, ay, az)
        self._anchor_yaw = anchor_yaw

        self._register_actor(ugv_actor)
        self.fleet.register(
            CarlaActorMember(entity_id="UGV-01", role="dispatchable", actor=ugv_actor)
        )
        self._register_entity("UGV-01")
        self.fleet.set_origin(
            "UGV-01", Pose(x=ax, y=ay, z=az, yaw=anchor_yaw)
        )
        self.event_log.add(
            "ok", "SCENARIO",
            f"UGV-01 spawned ({ugv_actor.type_id}) at ({ax:.1f}, {ay:.1f}, {az:.1f})",
        )

        origin = Pose(
            x=uav_spawn.x, y=uav_spawn.y, z=uav_spawn.z,
            yaw=uav_spawn.yaw,
        )
        uav = VirtualMember(
            entity_id="UAV-01", role="patrol",
            _pose=origin,
            altitude=origin.z, heading=origin.yaw, battery=100.0,
        )
        self.fleet.register(uav)
        self._register_entity("UAV-01")
        self.fleet.set_origin("UAV-01", origin)
        self.event_log.add(
            "ok", "SCENARIO",
            f"virtual UAV-01 registered at ({origin.x:.1f}, {origin.y:.1f}, {origin.z:.1f})",
        )

        # Bind cameras (FrameQueue instances stay across reset).
        self.camera_manager.rebind(
            "aerial", "UAV-01", world=carla_world, fleet=self.fleet,
        )
        self._record_rebound("aerial")
        self.camera_manager.rebind(
            "ground", "UGV-01", world=carla_world, fleet=self.fleet,
        )
        self._record_rebound("ground")
        self.event_log.add(
            "ok", "SCENARIO", "cameras bound: aerial→UAV-01, ground→UGV-01",
        )

    def teardown(self) -> None:
        # Drop follower first — it references the UGV actor about to be destroyed.
        self._ugv_follower = None
        self._fire_actors.clear()
        # Fleet bookkeeping that base.teardown does NOT touch (origins/incidents).
        self.fleet.clear_incidents()
        for eid in list(self.fleet.origins().keys()):
            # Leave it to fleet.unregister (called from base.teardown) to clean.
            _ = eid
        super().teardown()

    # ---- per-tick hooks ----------------------------------------------

    def on_tick_post(self, sim_time: float) -> None:
        if self._resetting:
            return
        dt = 1.0 / 30.0

        # 1. UAV lerp (per virtual UAV).
        for uav in self.fleet.virtual():
            uav.step(dt)

        # 2. UAV patrol — advance to next waypoint if arrived.
        for cmd_id, in_flt in list(self._in_flight.items()):
            if in_flt.awaiting not in ("patrol_finish", "ongoing"):
                continue
            if in_flt.kind != CommandKind.UAV_PATROL:
                continue
            self._advance_patrol(in_flt)

        # 3. UGV follower one step (light RPC: get_transform + get_velocity).
        if self._ugv_follower is not None:
            try:
                control = self._ugv_follower.run_step()
                ugv = self.fleet.get("UGV-01")
                if isinstance(ugv, CarlaActorMember):
                    ugv.actor.apply_control(control)
            except Exception:
                log.exception("S1: WaypointFollower.run_step failed")
                # Surface a single failure to any in-flight UGV cmd that
                # depends on the follower.
                for cmd_id, in_flt in list(self._in_flight.items()):
                    if in_flt.awaiting == "ugv_arrival":
                        self._finalize_command(
                            cmd_id, in_flt,
                            CompletionResult.failed("follower_error", {"message": "run_step raised"}),
                            sim_time,
                        )
                self._ugv_follower = None

        # 4. Drive lifecycle (instant / arrival / extinguish / patrol_finish).
        self._drive_command_lifecycle(sim_time)

    # ---- on_command --------------------------------------------------

    _HANDLERS = {
        CommandKind.UAV_PATROL: "_handle_uav_patrol",
        CommandKind.UAV_GOTO: "_handle_uav_goto",
        CommandKind.UAV_RTL: "_handle_uav_rtl",
        CommandKind.UAV_HOLD: "_handle_uav_hold",
        CommandKind.UGV_GOTO: "_handle_ugv_goto",
        CommandKind.UGV_RTL: "_handle_ugv_rtl",
        CommandKind.UGV_EXTINGUISH: "_handle_ugv_extinguish",
        CommandKind.UGV_STOP: "_handle_ugv_stop",
    }

    def on_command(self, cmd: Any) -> None:
        if self._resetting:
            raise RejectCommand("scenario_resetting")
        if not isinstance(cmd, ParsedCommand):
            raise RejectCommand(
                "internal_error",
                {"message": f"unknown command type {type(cmd).__name__}"},
            )
        handler_name = self._HANDLERS.get(cmd.kind)
        if handler_name is None:  # pragma: no cover — exhaustive over CommandKind
            raise RejectCommand(
                "internal_error", {"message": f"unhandled CommandKind {cmd.kind}"}
            )
        handler = getattr(self, handler_name)
        handler(cmd, self._current_sim_time())

    # ---- UAV handlers ------------------------------------------------

    def _handle_uav_patrol(self, cmd: ParsedCommand, sim_time: float) -> None:
        uav = self._require_uav(cmd.target)
        path = [Pose(x=p["x"], y=p["y"], z=p["z"]) for p in cmd.params["path"]]
        cruise = float(cmd.params["cruise_speed"])
        loop = bool(cmd.params.get("loop", False))
        awaiting = "ongoing" if loop else "patrol_finish"
        in_flt = self._accept_command(cmd, sim_time, awaiting=awaiting)
        in_flt.state.update({
            "path": path,
            "loop": loop,
            "cruise_speed": cruise,
            "index": 0,
        })
        uav.set_target(path[0], cruise_speed=cruise)
        if loop and self.command_bus is not None:
            # design §3.4 — accept of loop=true declares "no auto-completion".
            self.command_bus.broadcast_command_status({
                "cmd_id": cmd.id,
                "status": "ongoing",
                "kind": CommandKind.UAV_PATROL.value,
                "target": cmd.target,
                "reason": None,
                "detail": None,
                "at_sim_time": sim_time,
            })

    def _handle_uav_goto(self, cmd: ParsedCommand, sim_time: float) -> None:
        uav = self._require_uav(cmd.target)
        wp = cmd.params["waypoint"]
        target = Pose(x=wp["x"], y=wp["y"], z=wp["z"])
        cruise = float(cmd.params["cruise_speed"])
        in_flt = self._accept_command(cmd, sim_time, awaiting="uav_arrival")
        in_flt.state["target"] = target
        uav.set_target(target, cruise_speed=cruise)

    def _handle_uav_rtl(self, cmd: ParsedCommand, sim_time: float) -> None:
        uav = self._require_uav(cmd.target)
        origin = self.fleet.get_origin(cmd.target)
        if origin is None:
            raise RejectCommand("no_origin", {"target": cmd.target})
        cruise = float(cmd.params.get("cruise_speed", DEFAULT_UAV_RTL_SPEED))
        in_flt = self._accept_command(cmd, sim_time, awaiting="uav_arrival")
        in_flt.state["target"] = origin
        uav.set_target(origin, cruise_speed=cruise)

    def _handle_uav_hold(self, cmd: ParsedCommand, sim_time: float) -> None:
        uav = self._require_uav(cmd.target)
        self._accept_command(cmd, sim_time, awaiting="instant")
        uav.set_target(None)

    # ---- UGV handlers ------------------------------------------------

    def _handle_ugv_goto(self, cmd: ParsedCommand, sim_time: float) -> None:
        ugv = self._require_ugv(cmd.target)
        dest = cmd.params["dest"]
        dest_xyz = (float(dest["x"]), float(dest["y"]), float(dest["z"]))
        target_speed = self._resolve_ugv_target_speed(cmd.params.get("target_speed"))
        in_flt = self._accept_command(cmd, sim_time, awaiting="ugv_arrival")
        in_flt.state["dest"] = dest_xyz
        self._start_follower(ugv, dest_xyz, target_speed)

    def _handle_ugv_rtl(self, cmd: ParsedCommand, sim_time: float) -> None:
        ugv = self._require_ugv(cmd.target)
        origin = self.fleet.get_origin(cmd.target)
        if origin is None:
            raise RejectCommand("no_origin", {"target": cmd.target})
        target_speed = self._resolve_ugv_target_speed(cmd.params.get("target_speed"))
        in_flt = self._accept_command(cmd, sim_time, awaiting="ugv_arrival")
        in_flt.state["dest"] = (origin.x, origin.y, origin.z)
        self._start_follower(ugv, (origin.x, origin.y, origin.z), target_speed)

    def _handle_ugv_extinguish(self, cmd: ParsedCommand, sim_time: float) -> None:
        ugv = self._require_ugv(cmd.target)
        incident_id = cmd.params["incident_id"]
        incident = self.fleet.get_incident(incident_id)
        if incident is None:
            raise RejectCommand("unknown_incident", {"incident_id": incident_id})
        dist = ugv.pose().distance_to(incident.position)
        if dist > EXTINGUISH_RADIUS_M:
            raise RejectCommand(
                "not_in_range",
                {"distance_m": round(dist, 2), "max_m": EXTINGUISH_RADIUS_M},
            )
        in_flt = self._accept_command(cmd, sim_time, awaiting="extinguish")
        in_flt.state["incident_id"] = incident_id
        in_flt.state["extinguish_started_sim_time"] = sim_time

    def _handle_ugv_stop(self, cmd: ParsedCommand, sim_time: float) -> None:
        ugv = self._require_ugv(cmd.target)
        self._accept_command(cmd, sim_time, awaiting="instant")
        self._ugv_follower = None
        try:
            import carla
            ugv.actor.apply_control(carla.VehicleControl(throttle=0.0, brake=1.0, steer=0.0))
        except Exception:  # pragma: no cover — only happens without CARLA
            log.exception("UGV_STOP: apply_control(brake) failed")

    # ---- completion checks (override) --------------------------------

    def _check_completion(
        self, in_flt: InFlightCommand, sim_time: float
    ) -> CompletionResult | None:
        base_result = super()._check_completion(in_flt, sim_time)
        if base_result is not None:
            return base_result

        if in_flt.awaiting == "uav_arrival":
            uav = self.fleet.get(in_flt.target)
            if not isinstance(uav, VirtualMember):
                return CompletionResult.failed(
                    "entity_destroyed", {"target": in_flt.target}
                )
            target: Pose | None = in_flt.state.get("target")
            if target is None:
                return CompletionResult.failed(
                    "internal_error", {"message": "no recorded target"}
                )
            if uav.pose().distance_to(target) <= UAV_ARRIVAL_EPS_M:
                return CompletionResult.completed()
            return None

        if in_flt.awaiting == "ugv_arrival":
            if self._ugv_follower is None:
                return CompletionResult.failed(
                    "follower_error", {"message": "follower vanished"}
                )
            if self._ugv_follower.done():
                self._ugv_follower = None
                return CompletionResult.completed()
            return None

        if in_flt.awaiting == "patrol_finish":
            # Auto-completion happens in _advance_patrol when index passes end.
            if in_flt.state.get("finished"):
                return CompletionResult.completed()
            return None

        if in_flt.awaiting == "extinguish":
            started = float(in_flt.state["extinguish_started_sim_time"])
            if sim_time < started + EXTINGUISH_DWELL_S:
                return None
            incident_id = in_flt.state.get("incident_id")
            actor = self._fire_actors.pop(incident_id, None) if incident_id else None
            if actor is not None:
                try:
                    actor.destroy()
                except Exception:  # pragma: no cover — best-effort
                    log.exception("destroying fire actor %s failed", incident_id)
                if actor in self._spawned_actors:
                    self._spawned_actors.remove(actor)
            if incident_id:
                self.fleet.remove_incident(incident_id)
            return CompletionResult.completed()

        return None

    # ---- ignite_fire (HTTP entry) -----------------------------------

    def ignite_fire(
        self,
        *,
        id: str | None = None,
        position: dict | Pose,
        kind: str = "fire",
        severity: str = "high",
        blueprint: str | None = None,
    ) -> Incident:
        """Operator-triggered: spawn a fire actor + register an Incident.

        Raises:
            ValueError: ``id`` already in fleet.incidents
            RuntimeError: every candidate blueprint failed to spawn
        """
        if self._resetting:
            raise RuntimeError("scenario_resetting")
        if id is None:
            self._next_incident_seq += 1
            id = f"fire-{self._next_incident_seq:03d}"
        if self.fleet.get_incident(id) is not None:
            raise ValueError(f"incident_id already exists: {id}")
        pose = position if isinstance(position, Pose) else Pose(
            x=float(position["x"]),
            y=float(position["y"]),
            z=float(position.get("z", self._anchor_world_xyz[2])),
        )
        # Spawn the visual fire actor.
        bp_candidates = (blueprint,) if blueprint else FIRE_MARKER_BLUEPRINTS
        bp_candidates = tuple(b for b in bp_candidates if b is not None)
        fire_transform = _make_transform(
            x=pose.x, y=pose.y, z=pose.z, yaw=self._anchor_yaw,
        )
        actor = self._spawn_first_available(
            bp_candidates, fire_transform, kind="fire_marker"
        )
        if actor is None:
            raise RuntimeError(
                f"ignite_fire {id}: no blueprint spawnable from {bp_candidates}"
            )
        self._stabilize_fire_actor(actor)
        self._register_actor(actor)
        self._fire_actors[id] = actor
        incident = Incident(
            id=id, kind=kind, position=pose, severity=severity,
            since_sim_time=self._current_sim_time(),
        )
        self.fleet.add_incident(incident)
        self.event_log.add(
            "warn", "SCENARIO",
            f"incident {id} spawned at ({pose.x:.1f}, {pose.y:.1f}) severity={severity}",
        )
        return incident

    # ---- reset (HTTP entry) ------------------------------------------

    def reset(self) -> dict:
        """Operator-triggered: cancel everything, destroy actors, re-spawn.

        Returns a dict with cancelled cmd ids, destroyed incident ids, and the
        new run_id (design §5.2 response). Emits a ``scenario_event`` to
        ``/agent`` after reset completes (design §4.3).
        """
        if self._resetting:
            raise RuntimeError("scenario_resetting")
        self._resetting = True
        try:
            sim_time = self._current_sim_time()
            cancelled = self._cancel_all_in_flight(
                reason="reset", sim_time=sim_time, detail={"trigger": "http"},
            )
            destroyed_incidents = list(self.fleet.incidents().keys())
            self.teardown()
            self.setup()
            self._run_id += 1
        finally:
            self._resetting = False
        if self.command_bus is not None:
            self.command_bus.broadcast_scenario_event({
                "event": "reset",
                "run_id": self._run_id,
                "trigger": "http",
                "at_sim_time": self._current_sim_time(),
            })
        return {
            "cancelled_commands": cancelled,
            "destroyed_incidents": destroyed_incidents,
            "new_run_id": self._run_id,
        }

    # ---- helpers ------------------------------------------------------

    def _current_sim_time(self) -> float:
        """Best-effort sim_time accessor — runner injects on construction
        once R6 lands. R4 tests pass it explicitly; falls back to 0.0."""
        provider = getattr(self, "_sim_time_provider", None)
        try:
            return float(provider()) if callable(provider) else 0.0
        except Exception:
            return 0.0

    def attach_sim_time_provider(self, provider) -> None:
        """Wired by :class:`ScenarioRunner`."""
        self._sim_time_provider = provider

    def _require_uav(self, entity_id: str) -> VirtualMember:
        m = self.fleet.get(entity_id)
        if not isinstance(m, VirtualMember):
            raise RejectCommand("unknown_target", {"target": entity_id})
        return m

    def _require_ugv(self, entity_id: str) -> CarlaActorMember:
        m = self.fleet.get(entity_id)
        if not isinstance(m, CarlaActorMember):
            raise RejectCommand("unknown_target", {"target": entity_id})
        return m

    def _resolve_ugv_target_speed(self, raw: Any) -> float:
        kmh = float(raw) if raw is not None else DEFAULT_UGV_TARGET_SPEED_KMH
        return kmh / 3.6

    def _start_follower(
        self,
        ugv: CarlaActorMember,
        dest_xyz: tuple[float, float, float],
        target_speed_mps: float,
    ) -> None:
        follower = self._make_follower(ugv.actor, target_speed_mps)
        try:
            self._set_destination(follower, dest_xyz)
        except Exception as e:  # pragma: no cover — CARLA path
            raise RejectCommand(
                "internal_error",
                {"message": f"set_destination failed: {e}"},
            ) from e
        self._ugv_follower = follower

    def _make_follower(
        self, ugv_actor, target_speed_mps: float
    ) -> SimpleWaypointFollower:
        """Indirection so tests can inject a fake follower."""
        return SimpleWaypointFollower(
            ugv_actor, target_speed_mps=target_speed_mps,
        )

    def _set_destination(
        self,
        follower: SimpleWaypointFollower,
        dest_xyz: tuple[float, float, float],
    ) -> None:
        import carla

        follower.set_destination(
            self.world.carla_world,
            carla.Location(x=dest_xyz[0], y=dest_xyz[1], z=dest_xyz[2]),
        )

    def _advance_patrol(self, in_flt: InFlightCommand) -> None:
        uav = self.fleet.get(in_flt.target)
        if not isinstance(uav, VirtualMember):
            return
        path: list[Pose] = in_flt.state.get("path", [])
        if not path:
            return
        cruise = float(in_flt.state.get("cruise_speed", DEFAULT_UAV_RTL_SPEED))
        idx: int = in_flt.state.get("index", 0)
        if uav.pose().distance_to(path[idx]) > UAV_ARRIVAL_EPS_M:
            # Still in transit — keep current target armed.
            if uav.target is None:
                uav.set_target(path[idx], cruise_speed=cruise)
            return
        # Arrived at path[idx]; advance.
        next_idx = idx + 1
        if next_idx >= len(path):
            if in_flt.state.get("loop"):
                next_idx = 0
            else:
                in_flt.state["finished"] = True
                uav.set_target(None)
                return
        in_flt.state["index"] = next_idx
        uav.set_target(path[next_idx], cruise_speed=cruise)

    def _stabilize_fire_actor(self, actor: "carla.Actor") -> None:
        """Keep the visual marker fixed — vehicles would drift / collide."""
        try:
            import carla

            if isinstance(actor, carla.Vehicle):
                actor.set_autopilot(False)
                actor.set_simulate_physics(False)
                actor.apply_control(
                    carla.VehicleControl(throttle=0.0, brake=1.0, hand_brake=True)
                )
        except Exception:  # pragma: no cover — best-effort with real CARLA
            log.exception("stabilize fire actor failed for id=%s", actor.id)

    def _spawn_first_available(self, blueprint_ids, transform, *, kind: str):
        carla_world = self.world.carla_world
        blueprint_lib = carla_world.get_blueprint_library()
        for bp_id in blueprint_ids:
            matching = list(blueprint_lib.filter(bp_id))
            if not matching:
                log.debug("blueprint %s not present", bp_id)
                continue
            bp = matching[0]
            try:
                actor = carla_world.try_spawn_actor(bp, transform)
                if actor is None:
                    log.warning(
                        "%s: try_spawn_actor(%s) returned None (collision?)", kind, bp_id
                    )
                    continue
                log.info("%s spawned: bp=%s actor_id=%d", kind, bp_id, actor.id)
                return actor
            except Exception:
                log.exception("%s: spawn(%s) raised", kind, bp_id)
                continue
        return None


def _make_transform(*, x: float, y: float, z: float, yaw: float):
    import carla

    return carla.Transform(
        carla.Location(x=x, y=y, z=z),
        carla.Rotation(yaw=yaw),
    )


def _transform_pose(transform) -> tuple[float, float, float, float]:
    """(x, y, z, yaw) from a CARLA or test fake Transform."""
    loc = transform.location
    rot = transform.rotation
    return float(loc.x), float(loc.y), float(loc.z), float(rot.yaw)


def _spawn_ugv(carla_world, candidates, transform, *, label: str):
    """Try each blueprint at ``transform``; return (actor, transform, bp_id) or Nones."""
    for bp in candidates:
        try:
            actor = carla_world.try_spawn_actor(bp, transform)
        except Exception:
            log.exception("UGV: spawn(%s) at %s raised", bp.id, label)
            continue
        if actor is None:
            log.debug(
                "UGV: try_spawn_actor(%s) at %s returned None (collision?)",
                bp.id, label,
            )
            continue
        x, y, z, _ = _transform_pose(transform)
        log.info(
            "UGV spawned: bp=%s actor_id=%d at (%.1f, %.1f, %.1f) [%s]",
            bp.id, actor.id, x, y, z, label,
        )
        return actor, transform, bp.id
    return None, None, None


def _filter_existing(bp_lib, blueprint_ids):
    for bp_id in blueprint_ids:
        for bp in bp_lib.filter(bp_id):
            yield bp


__all__ = [
    "S1FireScenario",
    "UAV_ARRIVAL_EPS_M",
    "EXTINGUISH_RADIUS_M",
    "EXTINGUISH_DWELL_S",
    "DEFAULT_UAV_RTL_SPEED",
    "DEFAULT_UGV_TARGET_SPEED_KMH",
]
