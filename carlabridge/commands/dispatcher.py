"""Parse raw wire payloads into :class:`ParsedCommand` instances.

Schema (design §3.1 / §3.2)::

    {
      "id":       <non-empty str>,
      "kind":     <one of CommandKind values>,    # legacy "text" accepted
      "target":   <entity_id; "UAV-*" or "UGV-*">,
      "priority": <str; defaults "normal">,
      "params":   {...}                            # legacy "payload" accepted
    }

Failure modes (raise :class:`RejectCommand`):

* ``reason="parse_error"`` — schema or type problem; ``detail`` carries
  ``field`` / ``kind`` / ``message`` keys
* ``reason="kind_target_mismatch"`` — UAV kind sent to UGV-* (or vice versa)

Other rejection reasons (``unknown_target``, ``not_in_range``, …) come from
the scenario after parse, not from this layer.
"""

from __future__ import annotations

from typing import Any, Callable

from carlabridge.commands.enum import (
    UAV_KINDS,
    UGV_KINDS,
    CommandKind,
    ParsedCommand,
    RejectCommand,
)


def parse(raw: Any) -> ParsedCommand:
    if not isinstance(raw, dict):
        raise RejectCommand(
            "parse_error",
            {"message": f"payload must be an object, got {type(raw).__name__}"},
        )

    cmd_id = raw.get("id")
    if not isinstance(cmd_id, str) or not cmd_id:
        raise RejectCommand(
            "parse_error", {"field": "id", "message": "missing or invalid id"}
        )

    # Accept new `kind` field; fall back to legacy `text` for transitional clients.
    kind_str = raw.get("kind") if "kind" in raw else raw.get("text")
    if not isinstance(kind_str, str) or not kind_str:
        raise RejectCommand(
            "parse_error", {"field": "kind", "message": "missing or invalid kind"}
        )
    try:
        kind = CommandKind(kind_str)
    except ValueError as exc:
        raise RejectCommand(
            "parse_error",
            {"field": "kind", "message": f"unknown kind: {kind_str!r}"},
        ) from exc

    target = raw.get("target", "")
    if not isinstance(target, str) or not target:
        raise RejectCommand(
            "parse_error", {"field": "target", "message": "target required"}
        )

    if kind in UAV_KINDS and not target.startswith("UAV"):
        raise RejectCommand(
            "kind_target_mismatch", {"kind": kind.value, "target": target}
        )
    if kind in UGV_KINDS and not target.startswith("UGV"):
        raise RejectCommand(
            "kind_target_mismatch", {"kind": kind.value, "target": target}
        )

    priority = raw.get("priority", "normal")
    if not isinstance(priority, str):
        raise RejectCommand(
            "parse_error",
            {"field": "priority", "message": "priority must be a string"},
        )

    # Accept new `params` field; fall back to legacy `payload`.
    params: Any = raw.get("params") if "params" in raw else raw.get("payload", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise RejectCommand(
            "parse_error",
            {"field": "params", "message": "params must be an object"},
        )

    _VALIDATORS[kind](params)

    return ParsedCommand(
        id=cmd_id, kind=kind, target=target, priority=priority, params=params
    )


# ---- per-kind validators -------------------------------------------------


def _validate_uav_patrol(p: dict) -> None:
    path = p.get("path")
    if not isinstance(path, list) or not path:
        raise RejectCommand(
            "parse_error",
            {
                "kind": "UAV_PATROL",
                "field": "path",
                "message": "path must be a non-empty list of {x,y,z}",
            },
        )
    for i, wp in enumerate(path):
        if not _is_xyz(wp):
            raise RejectCommand(
                "parse_error",
                {
                    "kind": "UAV_PATROL",
                    "field": f"path[{i}]",
                    "message": "waypoint must have numeric x,y,z",
                },
            )
    if not _is_pos_number(p.get("cruise_speed")):
        raise RejectCommand(
            "parse_error",
            {
                "kind": "UAV_PATROL",
                "field": "cruise_speed",
                "message": "cruise_speed must be a positive number",
            },
        )
    loop = p.get("loop", False)
    if not isinstance(loop, bool):
        raise RejectCommand(
            "parse_error",
            {
                "kind": "UAV_PATROL",
                "field": "loop",
                "message": "loop must be a boolean",
            },
        )


def _validate_uav_goto(p: dict) -> None:
    if not _is_xyz(p.get("waypoint")):
        raise RejectCommand(
            "parse_error",
            {
                "kind": "UAV_GOTO",
                "field": "waypoint",
                "message": "waypoint must have numeric x,y,z",
            },
        )
    if not _is_pos_number(p.get("cruise_speed")):
        raise RejectCommand(
            "parse_error",
            {
                "kind": "UAV_GOTO",
                "field": "cruise_speed",
                "message": "cruise_speed must be a positive number",
            },
        )


def _validate_uav_rtl(p: dict) -> None:
    cs = p.get("cruise_speed")
    if cs is not None and not _is_pos_number(cs):
        raise RejectCommand(
            "parse_error",
            {
                "kind": "UAV_RTL",
                "field": "cruise_speed",
                "message": "cruise_speed must be a positive number when provided",
            },
        )


def _validate_uav_hold(_p: dict) -> None:
    return None


def _validate_ugv_goto(p: dict) -> None:
    if not _is_xyz(p.get("dest")):
        raise RejectCommand(
            "parse_error",
            {
                "kind": "UGV_GOTO",
                "field": "dest",
                "message": "dest must have numeric x,y,z",
            },
        )
    ts = p.get("target_speed")
    if ts is not None and not _is_pos_number(ts):
        raise RejectCommand(
            "parse_error",
            {
                "kind": "UGV_GOTO",
                "field": "target_speed",
                "message": "target_speed must be a positive number when provided",
            },
        )


def _validate_ugv_rtl(p: dict) -> None:
    ts = p.get("target_speed")
    if ts is not None and not _is_pos_number(ts):
        raise RejectCommand(
            "parse_error",
            {
                "kind": "UGV_RTL",
                "field": "target_speed",
                "message": "target_speed must be a positive number when provided",
            },
        )


def _validate_ugv_extinguish(p: dict) -> None:
    iid = p.get("incident_id")
    if not isinstance(iid, str) or not iid:
        raise RejectCommand(
            "parse_error",
            {
                "kind": "UGV_EXTINGUISH",
                "field": "incident_id",
                "message": "incident_id required",
            },
        )


def _validate_ugv_stop(_p: dict) -> None:
    return None


_VALIDATORS: dict[CommandKind, Callable[[dict], None]] = {
    CommandKind.UAV_PATROL: _validate_uav_patrol,
    CommandKind.UAV_GOTO: _validate_uav_goto,
    CommandKind.UAV_RTL: _validate_uav_rtl,
    CommandKind.UAV_HOLD: _validate_uav_hold,
    CommandKind.UGV_GOTO: _validate_ugv_goto,
    CommandKind.UGV_RTL: _validate_ugv_rtl,
    CommandKind.UGV_EXTINGUISH: _validate_ugv_extinguish,
    CommandKind.UGV_STOP: _validate_ugv_stop,
}


def _is_xyz(v: Any) -> bool:
    return (
        isinstance(v, dict)
        and isinstance(v.get("x"), (int, float))
        and not isinstance(v.get("x"), bool)
        and isinstance(v.get("y"), (int, float))
        and not isinstance(v.get("y"), bool)
        and isinstance(v.get("z"), (int, float))
        and not isinstance(v.get("z"), bool)
    )


def _is_pos_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


__all__ = ["parse"]
