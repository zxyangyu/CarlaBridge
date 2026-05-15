"""Protocol v1.0 envelope helpers (bridge-agent-protocol-v1.md §3.1 / §3.2).

All bridge → agent application-level events must be wrapped in the canonical
envelope:

    {
      "version": "1.0",
      "msg_id": "<uuid4>",
      "type": "<event-name>",
      "timestamp": <wall epoch seconds>,
      "frame": <tick count | None>,
      "sim_time": <CARLA sim seconds | None>,
      "sender": "bridge" | "agent",
      "payload": { ...event-specific... }
    }

Inbound events from the Agent may arrive either envelope-wrapped or as a bare
payload (protocol §3.2 — Bridge MUST tolerate both). ``unwrap`` collapses both
shapes into the inner payload dict.

The hello RPC return value and ``agent.command`` ack are NOT envelopes — they
are simple RPC responses per protocol §2.2 / §5.1.2.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

PROTOCOL_VERSION = "1.0"


def wrap(
    event_type: str,
    payload: dict,
    *,
    sim_time: float | None = None,
    frame: int | None = None,
    sender: str = "bridge",
) -> dict:
    """Build a protocol §3.1 envelope around ``payload``."""
    return {
        "version": PROTOCOL_VERSION,
        "msg_id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": time.time(),
        "frame": frame,
        "sim_time": sim_time,
        "sender": sender,
        "payload": payload,
    }


def unwrap(data: Any) -> dict:
    """Return the inner payload regardless of envelope presence (§3.2).

    Accepts:
      * Full envelope: ``{"version": ..., "payload": {...}, ...}`` → inner dict.
      * Bare payload: ``{"id": "...", "kind": "...", ...}`` → returned as-is.
      * Anything else → empty dict (the handler can then reject as parse_error).
    """
    if isinstance(data, dict):
        inner = data.get("payload")
        if isinstance(inner, dict):
            return inner
        return data
    return {}


__all__ = ["PROTOCOL_VERSION", "wrap", "unwrap"]
