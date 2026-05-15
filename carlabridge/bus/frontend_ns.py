"""Frontend Socket.IO namespace ('/').

On connect: send the current projected snapshot + replay last 100 events.

Refactor v0.3 (design §7.6): the frontend ``agent_command`` "suggestion"
route is removed. The frontend is a viewer; commands flow exclusively from
the Agent via ``sio.call('agent.command', ..., namespace='/agent')``.
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
        self._sids: set[str] = set()

    @property
    def client_count(self) -> int:
        return len(self._sids)

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> None:
        log.info("frontend connected sid=%s", sid)
        self._sids.add(sid)
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
        self._sids.discard(sid)
        self._event_log.add("info", "BRIDGE", f"frontend disconnected sid={sid}")
