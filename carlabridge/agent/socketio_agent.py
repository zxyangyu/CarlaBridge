"""SocketIOAgentLink — stub for `agent.mode = "remote"`.

When a real Urban Agent connects on `/agent`, this link mirrors the
mock contract but routes calls over the wire:

- `emit_command`:    `sio.emit('agent_command', cmd, namespace='/agent')` —
                     but in practice the remote agent is what PRODUCES
                     commands; this method is rarely used by mock.
- `emit_event_log`:  `sio.emit('event_log', ..., namespace='/agent')` so the
                     remote agent gets a unified event stream.
- `on_suggestion`:   `sio.emit('suggestion', payload, namespace='/agent')` —
                     the remote agent decides ack/reject.

M6 ships the stub so wiring picks the right link by config; M7+ may flesh out
remote behavior when an actual urban agent is hooked up.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from carlabridge.agent.link import AgentLink

if TYPE_CHECKING:  # pragma: no cover
    import socketio

    from carlabridge.obs.event_log import EventLog

log = logging.getLogger(__name__)


class SocketIOAgentLink(AgentLink):
    def __init__(self, *, sio: "socketio.AsyncServer", event_log: "EventLog") -> None:
        self._sio = sio
        self._event_log = event_log

    async def emit_command(self, cmd: dict) -> None:
        log.debug("SocketIOAgentLink.emit_command: echo to /agent: %s", cmd.get("id"))
        await self._sio.emit("agent_command", cmd, namespace="/agent")

    async def emit_event_log(
        self, severity: str, source: str, message: str
    ) -> None:
        # Store locally too so /healthz + new clients see it.
        self._event_log.add(severity, source, message)  # type: ignore[arg-type]
        await self._sio.emit(
            "event_log",
            {"severity": severity, "source": source, "message": message},
            namespace="/agent",
        )

    async def on_suggestion(self, payload: dict) -> None:
        # Frontend → remote agent: tag source explicitly per spec §7.2.
        envelope = {**payload, "source": "FRONTEND"}
        await self._sio.emit("suggestion", envelope, namespace="/agent")


__all__ = ["SocketIOAgentLink"]
