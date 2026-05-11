"""Agent Socket.IO namespace ('/agent').

On connect: send the current full WorldSnapshot (no projection — spec §7.2 D1).
M6 wires `agent_command` into commands.dispatcher + cross-domain queue.
"""

from __future__ import annotations

import logging

import socketio

from carlabridge.bus.projector import for_agent
from carlabridge.core.atomic import AtomicRef
from carlabridge.core.snapshot import WorldSnapshot
from carlabridge.obs.event_log import EventLog

log = logging.getLogger(__name__)


class AgentNamespace(socketio.AsyncNamespace):
    def __init__(
        self,
        namespace: str,
        *,
        event_log: EventLog,
        snapshot_ref: AtomicRef[WorldSnapshot],
    ) -> None:
        super().__init__(namespace)
        self._event_log = event_log
        self._snap_ref = snapshot_ref

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> None:
        log.info("agent connected sid=%s", sid)
        self._event_log.add("ok", "BRIDGE", f"agent connected sid={sid}")
        snap = self._snap_ref.get()
        if snap is not None:
            await self.emit("state_snapshot", for_agent(snap), to=sid)

    async def on_disconnect(self, sid: str) -> None:
        log.info("agent disconnected sid=%s", sid)
        self._event_log.add("info", "BRIDGE", f"agent disconnected sid={sid}")

    async def on_hello(self, sid: str, payload: dict) -> None:
        log.info("agent hello sid=%s payload=%s", sid, payload)
        self._event_log.add(
            "info", "BRIDGE", f"agent hello: {payload.get('agent_id', '?')}"
        )

    async def on_agent_command(self, sid: str, payload: dict) -> None:
        # M2: log only. M6 parses and routes into the sim-domain command queue.
        log.info("agent_command sid=%s payload=%s", sid, payload)
        self._event_log.add(
            "info",
            "AGENT",
            f"agent_command received (M6 will dispatch): {payload}",
        )

    async def on_event_log(self, sid: str, payload: dict) -> None:
        # Pass-through: agent's own decision logs.
        self._event_log.add(
            payload.get("severity", "info"),
            "AGENT",
            str(payload.get("message", "")),
        )
