"""Command enums + parsed dataclass + RejectCommand exception.

Wire format from `/agent` (refactor v0.3, design §3.1)::

    {
      "id": "cmd-9f1",
      "target": "UAV-01",
      "kind": "UAV_GOTO",
      "priority": "normal",
      "params": {"waypoint": {"x":..,"y":..,"z":..}, "cruise_speed": 8.0}
    }

`kind` parses into a :class:`CommandKind`. Scenarios receive
:class:`ParsedCommand`. Refusal happens by raising
``RejectCommand(reason, detail=...)`` — these become the ``rejected`` sio.call
return value (see design §3.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CommandKind(str, Enum):
    # UAV (virtual entities) — design §3.2
    UAV_PATROL = "UAV_PATROL"
    UAV_GOTO = "UAV_GOTO"
    UAV_RTL = "UAV_RTL"
    UAV_HOLD = "UAV_HOLD"
    # UGV (real CARLA actors)
    UGV_GOTO = "UGV_GOTO"
    UGV_RTL = "UGV_RTL"
    UGV_EXTINGUISH = "UGV_EXTINGUISH"
    UGV_STOP = "UGV_STOP"


UAV_KINDS: frozenset[CommandKind] = frozenset({
    CommandKind.UAV_PATROL,
    CommandKind.UAV_GOTO,
    CommandKind.UAV_RTL,
    CommandKind.UAV_HOLD,
})

UGV_KINDS: frozenset[CommandKind] = frozenset({
    CommandKind.UGV_GOTO,
    CommandKind.UGV_RTL,
    CommandKind.UGV_EXTINGUISH,
    CommandKind.UGV_STOP,
})


Priority = str  # "normal" | "high" | "urgent" — kept as str for forward compat.


@dataclass(slots=True)
class ParsedCommand:
    id: str
    kind: CommandKind
    target: str
    priority: Priority = "normal"
    params: dict[str, Any] = field(default_factory=dict)


class RejectCommand(Exception):
    """Raised by ``dispatcher.parse`` or ``scenario.on_command`` to refuse a
    command. Carries a machine-readable ``reason`` (see design §3.3 reason
    enum: ``parse_error`` / ``unknown_target`` / ``kind_target_mismatch`` /
    ``unknown_incident`` / ``not_in_range`` / ``no_origin`` /
    ``scenario_resetting`` / ``overloaded`` / ``internal_error``) and an
    optional ``detail`` dict.
    """

    def __init__(self, reason: str, detail: dict | None = None) -> None:
        super().__init__(reason)
        self.reason: str = reason
        self.detail: dict = dict(detail) if detail else {}

    def __str__(self) -> str:
        if self.detail:
            return f"{self.reason} {self.detail}"
        return self.reason

    def to_payload(self) -> dict:
        """Build the ``rejected`` shape returned by ``sio.call`` (design §3.3)."""
        return {"reason": self.reason, "detail": self.detail or None}


__all__ = [
    "CommandKind",
    "UAV_KINDS",
    "UGV_KINDS",
    "ParsedCommand",
    "Priority",
    "RejectCommand",
]
