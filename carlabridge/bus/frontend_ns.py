"""Frontend Socket.IO namespace ('/').

M0: connect/disconnect logging only. State broadcaster, agent_command relay,
and event_log replay all arrive in later milestones (see tasks.md).
"""

from __future__ import annotations

import logging

import socketio

from carlabridge.obs.event_log import EventLog

log = logging.getLogger(__name__)


class FrontendNamespace(socketio.AsyncNamespace):
    def __init__(self, namespace: str, *, event_log: EventLog) -> None:
        super().__init__(namespace)
        self._event_log = event_log

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> None:
        log.info("frontend connected sid=%s", sid)
        self._event_log.add("ok", "BRIDGE", f"frontend connected sid={sid}")

    async def on_disconnect(self, sid: str) -> None:
        log.info("frontend disconnected sid=%s", sid)
        self._event_log.add("info", "BRIDGE", f"frontend disconnected sid={sid}")

    async def on_agent_command(self, sid: str, payload: dict) -> None:
        # M0: log only. M6 implements `suggestion` relay to AgentLink.
        log.info("frontend suggestion sid=%s payload=%s", sid, payload)
        self._event_log.add(
            "info",
            "BRIDGE",
            f"frontend suggestion received (M6 will route to Agent): {payload}",
        )
