"""Parse raw wire payloads into ParsedCommand instances.

Validation rules:
- `id`         : required, non-empty string
- `text`       : required, must map to a CommandKind
- `target`     : required, non-empty for entity-targeted commands; allowed
                 empty for MARK_EVENT (which is scenario-wide)
- `priority`   : optional; defaults to 'normal'; only validated as a string
- `payload`    : optional dict; per-CommandKind shape checks below
"""

from __future__ import annotations

from typing import Any

from carlabridge.commands.enum import CommandKind, ParsedCommand, RejectCommand


def parse(raw: Any) -> ParsedCommand:
    if not isinstance(raw, dict):
        raise RejectCommand(f"payload must be an object, got {type(raw).__name__}")
    cmd_id = raw.get("id")
    if not isinstance(cmd_id, str) or not cmd_id:
        raise RejectCommand("missing or invalid `id`")
    text = raw.get("text")
    if not isinstance(text, str):
        raise RejectCommand("missing or invalid `text`")
    kind = CommandKind.from_text(text)
    target = raw.get("target", "")
    if not isinstance(target, str):
        raise RejectCommand("`target` must be a string")
    if kind != CommandKind.MARK_EVENT and not target:
        raise RejectCommand(f"`target` required for {kind.value}")
    priority = raw.get("priority", "normal")
    if not isinstance(priority, str):
        raise RejectCommand("`priority` must be a string")
    payload = raw.get("payload") or {}
    if not isinstance(payload, dict):
        raise RejectCommand("`payload` must be an object")

    _validate_payload(kind, payload)

    return ParsedCommand(
        id=cmd_id, kind=kind, target=target, priority=priority, payload=payload
    )


def _validate_payload(kind: CommandKind, payload: dict) -> None:
    if kind == CommandKind.UGV_DISPATCH:
        # Accept either {lat, lng} or {x, y, z}. Verify presence of one.
        has_latlng = "lat" in payload and "lng" in payload
        has_xyz = "x" in payload and "y" in payload
        if not (has_latlng or has_xyz):
            raise RejectCommand(
                "UGV_DISPATCH requires payload with either {lat,lng} or {x,y}"
            )
    # UGV_RTL: no payload (origin used by scenario)
    # UAV_RTL / UAV_HOLD: no payload
    # MARK_EVENT: payload may carry {severity, message}
    if kind == CommandKind.MARK_EVENT:
        sev = payload.get("severity", "info")
        if sev not in ("info", "ok", "warn", "danger"):
            raise RejectCommand(f"MARK_EVENT bad severity {sev!r}")


__all__ = ["parse"]
