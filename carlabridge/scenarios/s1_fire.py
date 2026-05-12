"""S1 — Fire emergency scenario (M6: end-to-end).

Lifecycle:
- setup(): spawn UGV + 3 virtual UAVs + fire marker + bind cameras
- on_tick_post(): step virtual UAVs + drive UGV via BasicAgent (when dispatched)
- on_command(): UAV_RTL/HOLD/MARK_EVENT inline; UGV_DISPATCH/RTL via BasicAgent
- mock_agent_loop(link): SCRIPT-driven (sim_time gated) events that emit
  through the SAME AgentLink a real urban agent would use

Actor layout (relative to `Town10HD_Opt.get_spawn_points()[0]` = anchor S):
    - UGV-01       : real CARLA vehicle at S            (dispatchable)
    - UAV-01/02/03 : virtual entities at (S.x±dx, S.y, S.z+60)  (patrol)
    - fire_marker  : static prop ~80m ahead of S        (visual anchor; D4)

Camera bindings (set in setup via CameraManager.rebind):
    aerial -> UAV-01  (follows_virtual, +20m above, pitch -30°)
    ground -> UGV-01  (attached_to_actor)
    city   -> world_pose overhead (bound by main, untouched here)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from carlabridge.commands.enum import CommandKind, ParsedCommand, RejectCommand
from carlabridge.core.fleet import CarlaActorMember, Pose, VirtualMember
from carlabridge.scenarios.base import Scenario, register_scenario
from carlabridge.scenarios.waypoint_follower import SimpleWaypointFollower

if TYPE_CHECKING:  # pragma: no cover
    from carlabridge.agent.link import AgentLink

log = logging.getLogger(__name__)


# ---------- tuning constants (Town10HD_Opt) -------------------------------

UGV_BLUEPRINT_CANDIDATES = (
    "vehicle.lincoln.mkz_2020",
    "vehicle.lincoln.mkz_2017",
    "vehicle.tesla.model3",
)

FIRE_MARKER_BLUEPRINTS = (
    "static.prop.streetbarrier",
    "static.prop.constructioncone",
    "static.prop.warningconstruction",
)

UAV_ALTITUDE = 60.0       # meters above the anchor
UAV_SPREAD = 20.0         # horizontal spread between UAVs (meters)
FIRE_DISTANCE = 80.0      # UGV → fire marker offset along +x (meters)
UGV_TARGET_SPEED = 25.0   # km/h passed to BasicAgent

# Polling interval for sim_time → wall_time gating inside mock_agent_loop.
SCRIPT_TICK_S = 0.05


# ---------- mock-agent script ---------------------------------------------


@dataclass(slots=True)
class ScriptEvent:
    at: float        # seconds of sim_time since scenario start
    kind: str        # "event_log" | "cmd"
    # event_log fields:
    severity: str = "info"
    message: str = ""
    # cmd fields:
    target: str = ""
    text: str = ""
    priority: str = "normal"
    payload: dict[str, Any] | None = None


# Spec §4.1 — 11-step fire emergency flow:
_SCRIPT: list[ScriptEvent] = [
    ScriptEvent(at=4.0, kind="event_log", severity="info",
                message="patrol started — three UAVs airborne"),
    ScriptEvent(at=6.0, kind="event_log", severity="warn",
                message="detected fire @ anchor +80m east"),
    ScriptEvent(at=7.0, kind="cmd", target="UAV-02", text="UAV_RTL",
                priority="high"),
    ScriptEvent(at=7.0, kind="cmd", target="UAV-03", text="UAV_RTL",
                priority="high"),
    ScriptEvent(at=8.0, kind="cmd", target="UAV-01", text="UAV_HOLD",
                priority="high"),
    ScriptEvent(at=9.0, kind="cmd", target="UGV-01", text="UGV_DISPATCH",
                priority="urgent",
                payload={"fire_distance": FIRE_DISTANCE}),
    ScriptEvent(at=25.0, kind="event_log", severity="ok",
                message="UGV arrived (mock: timed)"),
    ScriptEvent(at=27.0, kind="event_log", severity="ok",
                message="fire extinguished (D3: no robotic action — event only)"),
    ScriptEvent(at=29.0, kind="cmd", target="UGV-01", text="UGV_RTL",
                priority="normal"),
    ScriptEvent(at=45.0, kind="event_log", severity="ok",
                message="UGV returned"),
    ScriptEvent(at=46.0, kind="event_log", severity="ok",
                message="scenario complete"),
]


# ---------- scenario -------------------------------------------------------


@register_scenario("s1_fire")
class S1FireScenario(Scenario):
    """3 UAVs + 1 UGV + 1 fire marker; cameras bound to UAV-01 / UGV-01."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # UGV state: non-None when a SimpleWaypointFollower is in flight.
        self._ugv_follower: SimpleWaypointFollower | None = None
        self._ugv_arrived_announced = False
        self._ugv_origin: Pose | None = None
        self._anchor_world_xyz: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._anchor_yaw: float = 0.0
        # UAV origin poses for RTL.
        self._uav_origins: dict[str, Pose] = {}
        # Script timing: scenario start sim_time, captured at start of
        # mock_agent_loop (NOT at setup) so the timer starts when the loop
        # actually begins.
        self._script_start_sim: float | None = None
        self._sim_time_provider = None  # injected by ScenarioRunner / main

    # Public — set by ScenarioRunner so mock_agent_loop can gate on sim_time.
    def attach_sim_time_provider(self, provider) -> None:
        self._sim_time_provider = provider

    # ---- setup / teardown ---------------------------------------------

    def setup(self) -> None:
        carla_world = self.world.carla_world

        spawn_points = carla_world.get_map().get_spawn_points()
        if not spawn_points:
            raise RuntimeError("Town10HD_Opt: no spawn points returned")
        log.info("S1: %d spawn points available", len(spawn_points))

        # ---- UGV: walk spawn_points + every vehicle.* blueprint -------
        bp_lib = carla_world.get_blueprint_library()
        # Preferred families first, then everything matching vehicle.*.
        preferred = list(_filter_existing(bp_lib, UGV_BLUEPRINT_CANDIDATES))
        all_vehicles = list(bp_lib.filter("vehicle.*"))
        # De-dup while preserving order: preferred first, then any other vehicle.
        seen_ids: set[str] = set()
        candidates = []
        for bp in preferred + all_vehicles:
            if bp.id in seen_ids:
                continue
            seen_ids.add(bp.id)
            candidates.append(bp)
        log.info(
            "S1: trying %d vehicle blueprint(s) across %d spawn point(s)",
            len(candidates), len(spawn_points),
        )

        ugv_actor = None
        anchor = None
        for tf in spawn_points:
            for bp in candidates:
                try:
                    actor = carla_world.try_spawn_actor(bp, tf)
                except Exception:
                    log.exception("UGV: spawn(%s) raised", bp.id)
                    continue
                if actor is not None:
                    ugv_actor = actor
                    anchor = tf
                    log.info(
                        "UGV spawned: bp=%s actor_id=%d at spawn_point (%.1f, %.1f, %.1f)",
                        bp.id, actor.id,
                        tf.location.x, tf.location.y, tf.location.z,
                    )
                    break
            if ugv_actor is not None:
                break
        if ugv_actor is None or anchor is None:
            raise RuntimeError(
                f"S1: no UGV could be spawned ({len(candidates)} blueprints × "
                f"{len(spawn_points)} spawn points all failed — every spot "
                f"appears occupied or no vehicle blueprints present)"
            )
        ax, ay, az = anchor.location.x, anchor.location.y, anchor.location.z
        self._anchor_world_xyz = (ax, ay, az)
        self._anchor_yaw = anchor.rotation.yaw
        log.info(
            "S1: anchor at (%.2f, %.2f, %.2f) yaw=%.1f",
            ax, ay, az, anchor.rotation.yaw,
        )

        self._register_actor(ugv_actor)
        self.fleet.register(
            CarlaActorMember(entity_id="UGV-01", role="dispatchable", actor=ugv_actor)
        )
        self._register_entity("UGV-01")
        # Save UGV origin for RTL.
        self._ugv_origin = Pose(x=ax, y=ay, z=az, yaw=anchor.rotation.yaw)
        self.event_log.add(
            "ok", "SCENARIO",
            f"UGV-01 spawned ({ugv_actor.type_id}) at ({ax:.1f}, {ay:.1f}, {az:.1f})",
        )

        # ---- 3 virtual UAVs ------------------------------------------
        for i, dx in enumerate((-UAV_SPREAD, 0.0, UAV_SPREAD), start=1):
            eid = f"UAV-0{i}"
            origin = Pose(
                x=ax + dx, y=ay, z=az + UAV_ALTITUDE,
                yaw=anchor.rotation.yaw,
            )
            self._uav_origins[eid] = origin
            uav = VirtualMember(
                entity_id=eid, role="patrol",
                _pose=origin,
                altitude=origin.z, heading=origin.yaw, battery=100.0,
            )
            self.fleet.register(uav)
            self._register_entity(eid)
        self.event_log.add(
            "ok", "SCENARIO",
            f"3 virtual UAVs registered at altitude {UAV_ALTITUDE}m",
        )

        # ---- fire marker (D4: anchor only) ----------------------------
        fire_transform = _make_transform(
            x=ax + FIRE_DISTANCE, y=ay, z=az + 0.5, yaw=anchor.rotation.yaw,
        )
        fire_actor = self._spawn_first_available(
            FIRE_MARKER_BLUEPRINTS, fire_transform, kind="fire_marker"
        )
        if fire_actor is not None:
            self._register_actor(fire_actor)
            self.event_log.add(
                "warn", "SCENARIO",
                f"fire marker placed at ({fire_transform.location.x:.1f}, "
                f"{fire_transform.location.y:.1f}) — D4 visual anchor only",
            )

        # ---- bind cameras --------------------------------------------
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
        # Drop follower first — it holds a reference to the UGV actor which
        # is about to be destroyed by super().teardown().
        self._ugv_follower = None
        super().teardown()

    # ---- per-tick hooks ----------------------------------------------

    def on_tick_post(self, sim_time: float) -> None:
        # Advance virtual UAV motion (lerp toward their `target`, if any).
        dt = 1.0 / 30.0
        for uav in self.fleet.virtual():
            uav.step(dt)
        # Drive UGV via SimpleWaypointFollower when a DISPATCH/RTL is active.
        # The follower only does light RPCs (get_transform + get_velocity +
        # apply_control), avoiding the BasicAgent RPC storm.
        if self._ugv_follower is not None:
            try:
                control = self._ugv_follower.run_step()
                ugv_member = self.fleet.get("UGV-01")
                if isinstance(ugv_member, CarlaActorMember):
                    ugv_member.actor.apply_control(control)
                if self._ugv_follower.done() and not self._ugv_arrived_announced:
                    self.event_log.add(
                        "ok", "SCENARIO", "UGV-01 arrived at destination",
                    )
                    self._ugv_arrived_announced = True
                    self._ugv_follower = None
            except Exception:
                log.exception("S1: WaypointFollower.run_step failed; clearing")
                self._ugv_follower = None
                self.event_log.add(
                    "danger", "SCENARIO",
                    "UGV-01 follower crashed; cleared (UGV will coast)",
                )

    # ---- on_command --------------------------------------------------

    def on_command(self, cmd: Any) -> None:
        if not isinstance(cmd, ParsedCommand):
            raise RejectCommand(f"unknown command type {type(cmd).__name__}")
        if cmd.kind == CommandKind.UAV_RTL:
            self._uav_rtl(cmd.target)
        elif cmd.kind == CommandKind.UAV_HOLD:
            self._uav_hold(cmd.target)
        elif cmd.kind == CommandKind.UGV_DISPATCH:
            self._ugv_dispatch(cmd)
        elif cmd.kind == CommandKind.UGV_RTL:
            self._ugv_rtl(cmd)
        elif cmd.kind == CommandKind.MARK_EVENT:
            severity = cmd.payload.get("severity", "info")
            message = cmd.payload.get("message", "(mark_event)")
            self.event_log.add(severity, "SCENARIO", message)  # type: ignore[arg-type]
        elif cmd.kind == CommandKind.ATTACH_ACTOR:
            # D3: not implemented this milestone.
            self.event_log.add(
                "info", "SCENARIO",
                f"ATTACH_ACTOR for {cmd.target} acknowledged "
                f"(D3: no robotic action this milestone)",
            )
        else:  # pragma: no cover -- exhaustive
            raise RejectCommand(f"unhandled CommandKind {cmd.kind}")

    # ---- command handlers --------------------------------------------

    def _uav_rtl(self, target: str) -> None:
        uav = self.fleet.get(target)
        if not isinstance(uav, VirtualMember):
            raise RejectCommand(f"UAV {target!r} not in fleet or not virtual")
        origin = self._uav_origins.get(target)
        if origin is None:
            raise RejectCommand(f"UAV {target!r} has no recorded origin")
        # RTL = climb to safe altitude already by spawn; just lerp back to origin.
        uav.set_target(origin, cruise_speed=15.0)
        self.event_log.add(
            "info", "SCENARIO", f"{target} RTL → ({origin.x:.1f}, {origin.y:.1f}, {origin.z:.1f})",
        )

    def _uav_hold(self, target: str) -> None:
        uav = self.fleet.get(target)
        if not isinstance(uav, VirtualMember):
            raise RejectCommand(f"UAV {target!r} not in fleet or not virtual")
        uav.set_target(None)
        self.event_log.add("info", "SCENARIO", f"{target} HOLD (target cleared)")

    def _ugv_dispatch(self, cmd: ParsedCommand) -> None:
        ugv = self.fleet.get(cmd.target)
        if not isinstance(ugv, CarlaActorMember):
            raise RejectCommand(f"UGV {cmd.target!r} not in fleet")
        dest = self._resolve_ugv_destination(cmd.payload)
        self._start_follower(ugv, dest)
        self.event_log.add(
            "info", "SCENARIO",
            f"{cmd.target} DISPATCH → ({dest[0]:.1f}, {dest[1]:.1f}, {dest[2]:.1f}) "
            f"[{self._ugv_follower.waypoint_count} waypoints]",
        )

    def _ugv_rtl(self, cmd: ParsedCommand) -> None:
        ugv = self.fleet.get(cmd.target)
        if not isinstance(ugv, CarlaActorMember):
            raise RejectCommand(f"UGV {cmd.target!r} not in fleet")
        if self._ugv_origin is None:
            raise RejectCommand("UGV origin not recorded")
        dest = (self._ugv_origin.x, self._ugv_origin.y, self._ugv_origin.z)
        self._start_follower(ugv, dest)
        self.event_log.add(
            "info", "SCENARIO",
            f"{cmd.target} RTL → ({dest[0]:.1f}, {dest[1]:.1f}, {dest[2]:.1f}) "
            f"[{self._ugv_follower.waypoint_count} waypoints]",
        )

    def _start_follower(
        self, ugv: CarlaActorMember, dest_xyz: tuple[float, float, float]
    ) -> None:
        """Construct a SimpleWaypointFollower and arm it. Resets arrival flag."""
        follower = self._make_follower(ugv.actor)
        try:
            self._set_destination(follower, dest_xyz)
        except Exception as e:
            log.exception("S1: follower.set_destination failed")
            raise RejectCommand(f"WaypointFollower set_destination failed: {e}") from e
        self._ugv_follower = follower
        self._ugv_arrived_announced = False

    def _make_follower(self, ugv_actor) -> SimpleWaypointFollower:
        """Indirection so tests can inject a fake follower."""
        return SimpleWaypointFollower(ugv_actor)

    # ---- helpers ------------------------------------------------------

    def _resolve_ugv_destination(self, payload: dict) -> tuple[float, float, float]:
        ax, ay, az = self._anchor_world_xyz
        if "x" in payload and "y" in payload:
            return (float(payload["x"]), float(payload["y"]), float(payload.get("z", az)))
        if "fire_distance" in payload:
            return (ax + float(payload["fire_distance"]), ay, az)
        if "lat" in payload and "lng" in payload:
            # M6 doesn't yet do CARLA geo → world. Treat as world coords as
            # a placeholder; M7 wires `get_map().get_geo_location()`.
            return (float(payload["lat"]), float(payload["lng"]), az)
        # Default: drive to the fire marker.
        return (ax + FIRE_DISTANCE, ay, az)

    def _set_destination(
        self, follower: SimpleWaypointFollower, dest_xyz: tuple[float, float, float]
    ) -> None:
        """Configure the follower's destination. Splittable for testing."""
        import carla

        follower.set_destination(
            self.world.carla_world,
            carla.Location(x=dest_xyz[0], y=dest_xyz[1], z=dest_xyz[2]),
        )

    # ---- spawn helpers ------------------------------------------------

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

    # ---- mock agent loop ----------------------------------------------

    async def mock_agent_loop(self, link: "AgentLink") -> None:
        """Coroutine that walks SCRIPT and fires events through AgentLink."""
        if self._sim_time_provider is None:
            log.warning("mock_agent_loop: no sim_time_provider; using 0")
            sim_time = lambda: 0.0  # noqa: E731
        else:
            sim_time = self._sim_time_provider
        self._script_start_sim = sim_time()
        self.event_log.add(
            "info", "AGENT", f"mock agent started @ sim_time={self._script_start_sim:.2f}",
        )

        for ev in _SCRIPT:
            target_sim = self._script_start_sim + ev.at
            while sim_time() < target_sim:
                await asyncio.sleep(SCRIPT_TICK_S)
            try:
                await self._fire_script_event(link, ev)
            except Exception:
                log.exception("mock_agent_loop: event at=%s failed", ev.at)

        self.event_log.add("ok", "AGENT", "mock agent script complete")

    async def _fire_script_event(self, link: "AgentLink", ev: ScriptEvent) -> None:
        if ev.kind == "event_log":
            await link.emit_event_log(ev.severity, "AGENT", ev.message)
            return
        if ev.kind == "cmd":
            cmd_id = f"mock-{ev.at:05.1f}-{ev.target or 'any'}-{ev.text}"
            payload = self._materialize_payload(ev.payload or {})
            await link.emit_command({
                "id": cmd_id,
                "target": ev.target,
                "priority": ev.priority,
                "text": ev.text,
                "payload": payload,
            })
            return
        log.warning("mock script: unknown event kind %r", ev.kind)

    def _materialize_payload(self, payload: dict) -> dict:
        """Translate SCRIPT-only sugar keys into wire-protocol-valid payloads.

        The SCRIPT is anchor-agnostic (anchor is only known at setup time), so
        it uses logical references like `fire_distance`. The dispatcher (which
        validates incoming wire payloads) only knows `{lat,lng}` or `{x,y}`,
        so we resolve here before crossing the AgentLink boundary.
        """
        if "fire_distance" not in payload:
            return payload
        ax, ay, _ = self._anchor_world_xyz
        out = {k: v for k, v in payload.items() if k != "fire_distance"}
        out["x"] = ax + float(payload["fire_distance"])
        out["y"] = ay
        return out


def _make_transform(*, x: float, y: float, z: float, yaw: float):
    import carla

    return carla.Transform(
        carla.Location(x=x, y=y, z=z),
        carla.Rotation(yaw=yaw),
    )


def _filter_existing(bp_lib, blueprint_ids):
    """Yield blueprints whose id matches anything in `blueprint_ids`.

    `bp_lib.find(id)` raises if not present, so we iterate matches via filter.
    """
    for bp_id in blueprint_ids:
        for bp in bp_lib.filter(bp_id):
            yield bp


__all__ = ["S1FireScenario"]
