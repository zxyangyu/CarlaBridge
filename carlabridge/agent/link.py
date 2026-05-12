"""AgentLink — abstract façade for `mock` vs `remote` agent control source.

The Bridge's scenario code is written against this interface. Mock and real
Agent implementations differ only in where their `emit_command` ends up:

  * MockAgentLink:     parses + queues directly into the CommandBus
  * SocketIOAgentLink: emits the command over `/agent` so the remote agent
                       observes it (the remote agent itself is what makes the
                       decision in `remote` mode, so this is mostly a no-op
                       echo path; M6 ships a stub).

`on_suggestion` is the bridge for frontend `agent_command` events (spec
§7.1.2 + §3.4) — the frontend is read-only on control, so its commands are
treated as proposals that the agent decides to accept or reject.
"""

from __future__ import annotations

import abc
from typing import Any


class AgentLink(abc.ABC):
    @abc.abstractmethod
    async def emit_command(self, cmd: dict) -> None:
        """Submit a command originating from the agent. Wire-format dict
        (id/target/priority/text/payload). Routed through the same code path
        a real Agent would use, so downstream is identical mock vs remote.
        """

    @abc.abstractmethod
    async def emit_event_log(
        self, severity: str, source: str, message: str
    ) -> None:
        """Publish an event_log entry (agent decision rationale, observations)."""

    @abc.abstractmethod
    async def on_suggestion(self, payload: dict) -> None:
        """Frontend → Agent: a `suggestion` (the frontend's `agent_command`
        is treated as such). The implementation decides whether to accept
        (forward through emit_command) or reject (emit agent_reject)."""


__all__ = ["AgentLink"]
