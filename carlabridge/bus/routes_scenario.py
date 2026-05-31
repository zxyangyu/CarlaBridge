"""HTTP control plane for the scenario (refactor v0.3, design §5).

Three operator-facing endpoints, all stateless and unauthenticated (CORS
inherits from ``settings.server.cors_origins``):

* ``POST /scenario/fire``   — spawn an :class:`Incident`
* ``POST /scenario/reset``  — teardown + setup, bump ``run_id``
* ``GET  /scenario/status`` — read-only state introspection

Routes are registered by :func:`register_routes`. They access the scenario
runner via ``request.app["late"]["scenario_runner"]`` so initialisation
order in ``main.py`` stays unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from carlabridge.config import FireMarkerCfg, Settings

if TYPE_CHECKING:  # pragma: no cover
    from carlabridge.scenarios.runner import ScenarioRunner

log = logging.getLogger(__name__)


def register_routes(app: web.Application) -> None:
    """Attach scenario routes to ``app``. Called from :func:`build_app`."""
    app.router.add_post("/scenario/fire", _fire)
    app.router.add_post("/scenario/reset", _reset)
    app.router.add_get("/scenario/status", _status)


# ---- helpers --------------------------------------------------------------


def _get_runner(request: web.Request) -> "ScenarioRunner | None":
    return request.app["late"].get("scenario_runner")


def _need_runner(request: web.Request) -> "ScenarioRunner | web.Response":
    runner = _get_runner(request)
    if runner is None or runner.scenario is None:
        return web.json_response(
            {"error": "scenario not running"}, status=503,
        )
    return runner


def _err(status: int, reason: str, **detail) -> web.Response:
    body: dict = {"status": "error", "reason": reason}
    if detail:
        body["detail"] = detail
    return web.json_response(body, status=status)


def _resolve_fire_position(
    markers: list[FireMarkerCfg],
    *,
    incident_id: str | None,
    existing_count: int,
) -> dict[str, float] | None:
    """Pick a fire marker from config; request-body position is ignored."""
    if not markers:
        return None

    if incident_id:
        for marker in markers:
            if marker.id and marker.id == incident_id:
                return {"x": marker.x, "y": marker.y, "z": marker.z}
        if incident_id.startswith("fire-"):
            try:
                idx = int(incident_id.rsplit("-", 1)[-1]) - 1
                if 0 <= idx < len(markers):
                    marker = markers[idx]
                    return {"x": marker.x, "y": marker.y, "z": marker.z}
            except ValueError:
                pass

    idx = min(existing_count, len(markers) - 1)
    marker = markers[idx]
    return {"x": marker.x, "y": marker.y, "z": marker.z}


# ---- POST /scenario/fire --------------------------------------------------


async def _fire(request: web.Request) -> web.Response:
    runner = _need_runner(request)
    if isinstance(runner, web.Response):
        return runner
    if runner.is_resetting():
        return _err(503, "scenario_resetting")
    try:
        body = await request.json()
    except Exception:
        return _err(400, "parse_error", message="body must be JSON")
    if not isinstance(body, dict):
        return _err(400, "parse_error", message="body must be a JSON object")

    settings: Settings = request.app["settings"]
    position = _resolve_fire_position(
        settings.scenario.fire_markers,
        incident_id=body.get("id"),
        existing_count=len(runner.scenario.fleet.incidents()),
    )
    if position is None:
        return _err(503, "no_fire_markers_configured")

    kwargs = {
        "id": body.get("id"),
        "position": position,
        "kind": body.get("kind", "fire"),
        "severity": body.get("severity", "high"),
        "blueprint": body.get("blueprint"),
    }

    try:
        incident = await runner.run_in_sim_domain(
            runner.scenario.ignite_fire, **kwargs
        )
    except ValueError as e:
        # Duplicate incident id.
        return _err(409, "duplicate_incident", message=str(e))
    except RuntimeError as e:
        # Either "scenario_resetting" or blueprint exhaustion.
        msg = str(e)
        if msg == "scenario_resetting":
            return _err(503, "scenario_resetting")
        return _err(409, "spawn_failed", message=msg)

    return web.json_response({
        "status": "ok",
        "incident_id": incident.id,
        "kind": incident.kind,
        "severity": incident.severity,
        "position": {
            "x": incident.position.x,
            "y": incident.position.y,
            "z": incident.position.z,
        },
        "since_sim_time": incident.since_sim_time,
        "run_id": int(getattr(runner.scenario, "_run_id", 0)),
    })


# ---- POST /scenario/reset -------------------------------------------------


async def _reset(request: web.Request) -> web.Response:
    runner = _need_runner(request)
    if isinstance(runner, web.Response):
        return runner
    if runner.is_resetting():
        return _err(503, "scenario_resetting")
    try:
        result = await runner.run_in_sim_domain(runner.scenario.reset)
    except RuntimeError as e:
        # Race: another reset got into sim domain first.
        if str(e) == "scenario_resetting":
            return _err(503, "scenario_resetting")
        log.exception("reset failed")
        return _err(500, "internal_error", message=str(e))
    except Exception as e:  # pragma: no cover -- safety net
        log.exception("reset raised unexpected")
        return _err(500, "internal_error", message=str(e))

    return web.json_response({
        "status": "ok",
        "run_id": result["new_run_id"],
        "cancelled_commands": result["cancelled_commands"],
        "destroyed_incidents": result["destroyed_incidents"],
    })


# ---- GET /scenario/status -------------------------------------------------


async def _status(request: web.Request) -> web.Response:
    runner = _get_runner(request)
    if runner is None:
        return web.json_response({
            "name": None,
            "run_id": 0,
            "bridge_session_id": "",
            "sim_time": 0.0,
            "resetting": False,
            "state": "none",
            "incidents": [],
            "in_flight_commands": [],
            "entities": {},
        })

    scenario = runner.scenario
    agent_ns = request.app.get("agent_ns")
    session_id = getattr(agent_ns, "bridge_session_id", "") if agent_ns else ""

    if scenario is None:
        return web.json_response({
            "name": runner.name,
            "run_id": 0,
            "bridge_session_id": session_id,
            "sim_time": runner.sim_time(),
            "resetting": False,
            "state": runner.state,
            "incidents": [],
            "in_flight_commands": [],
            "entities": {},
        })

    fleet = scenario.fleet
    incidents = [inc.to_wire() for _id, inc in sorted(fleet.incidents().items())]
    in_flight = scenario.in_flight_snapshot()
    entities: dict[str, dict] = {}
    origins = fleet.origins()
    for eid, member in [(m.entity_id, m) for m in fleet.all()]:
        pose = member.pose()
        entry: dict = {
            "current_pose": {"x": pose.x, "y": pose.y, "z": pose.z, "yaw": pose.yaw},
        }
        origin = origins.get(eid)
        if origin is not None:
            entry["origin"] = {"x": origin.x, "y": origin.y, "z": origin.z, "yaw": origin.yaw}
        entities[eid] = entry

    return web.json_response({
        "name": runner.name,
        "run_id": int(getattr(scenario, "_run_id", 0)),
        "bridge_session_id": session_id,
        "sim_time": runner.sim_time(),
        "resetting": bool(getattr(scenario, "_resetting", False)),
        "state": runner.state,
        "incidents": incidents,
        "in_flight_commands": in_flight,
        "entities": entities,
    })


__all__ = ["register_routes"]
