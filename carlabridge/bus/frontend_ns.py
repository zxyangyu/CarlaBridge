"""Frontend Socket.IO namespace ('/').

On connect: send the current projected snapshot immediately (so the LIVE
status bar lights up before the 100ms broadcaster tick) + replay the recent
event_log buffer.

`agent_command` from the frontend is `suggestion` semantics (spec D4):
M2 logs it; M6 routes through AgentLink.
"""

from __future__ import annotations

import logging

import socketio

from carlabridge.bus.projector import FocusBinding, for_frontend
from carlabridge.core.atomic import AtomicRef
from carlabridge.core.snapshot import WorldSnapshot
from carlabridge.obs.event_log import EventLog

log = logging.getLogger(__name__)


class FrontendNamespace(socketio.AsyncNamespace):
    def __init__(
        self,
        namespace: str,
        *,
        event_log: EventLog,
        snapshot_ref: AtomicRef[WorldSnapshot],
        focus: FocusBinding,
    ) -> None:
        super().__init__(namespace)
        self._event_log = event_log
        self._snap_ref = snapshot_ref
        self._focus = focus

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> None:
        log.info("frontend connected sid=%s", sid)
        self._event_log.add("ok", "BRIDGE", f"frontend connected sid={sid}")
        # Replay recent events so the new client lands with context.
        for evt in self._event_log.recent(100):
            await self.emit(
                "event_log",
                {"severity": evt.severity, "source": evt.source, "message": evt.message},
                to=sid,
            )
        # Emit an immediate snapshot if one is available.
        snap = self._snap_ref.get()
        if snap is not None:
            await self.emit("state_update", for_frontend(snap, self._focus), to=sid)

    async def on_disconnect(self, sid: str) -> None:
        log.info("frontend disconnected sid=%s", sid)
        self._event_log.add("info", "BRIDGE", f"frontend disconnected sid={sid}")

    async def on_agent_command(self, sid: str, payload: dict) -> None:
        # M2: log only. M6 implements `suggestion` relay to AgentLink.
        log.info("frontend suggestion sid=%s payload=%s", sid, payload)
        self._event_log.add(
            "info",
            "BRIDGE",
            f"frontend suggestion received (M6 will route to Agent): {payload}",
        )
