"""Command enums + parsed dataclass + RejectCommand exception.

The wire format from `/agent` is:
    {"id": "cmd-9f1", "target": "UGV-01", "priority": "high",
     "text": "UGV_DISPATCH", "payload": {"lat": 31.23, "lng": 121.47}}

`text` is parsed into a CommandKind. Scenarios receive `ParsedCommand` and
raise `RejectCommand(reason)` to refuse — that translates to `agent_reject`
on the wire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CommandKind(str, Enum):
    # UAV (virtual entities)
    UAV_RTL = "UAV_RTL"
    UAV_HOLD = "UAV_HOLD"
    # UGV (real CARLA actors)
    UGV_DISPATCH = "UGV_DISPATCH"
    UGV_RTL = "UGV_RTL"
    # Misc
    MARK_EVENT = "MARK_EVENT"
    ATTACH_ACTOR = "ATTACH_ACTOR"  # D3: parsed but no-op this milestone

    @classmethod
    def from_text(cls, text: str) -> "CommandKind":
        try:
            return cls(text)
        except ValueError as e:
            raise RejectCommand(f"unknown command text: {text!r}") from e


Priority = str  # "normal" | "high" | "urgent" — kept as str for forward compat.


@dataclass(slots=True)
class ParsedCommand:
    id: str
    kind: CommandKind
    target: str  # entity_id (UAV-01, UGV-01, ...) — may be "" for non-targeted
    priority: Priority = "normal"
    payload: dict[str, Any] = field(default_factory=dict)

    def to_ack(self, *, latency_ms: int = 0) -> dict:
        return {"id": self.id, "target": self.target, "latency_ms": latency_ms}

    def to_reject(self, reason: str) -> dict:
        return {"id": self.id, "target": self.target, "reason": reason}


class RejectCommand(Exception):
    """Raised inside `dispatcher.parse` or `scenario.on_command` to indicate
    the command should be rejected. The string message becomes the reject
    payload's `reason` field.
    """


__all__ = ["CommandKind", "ParsedCommand", "RejectCommand", "Priority"]
