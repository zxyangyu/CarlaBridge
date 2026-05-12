"""CommandBus — cross-domain pipe for agent commands.

Producers (async domain):
    bus.submit(parsed_cmd)            # raises queue.Full on overflow

Consumer (sim/tick domain):
    for cmd in bus.drain():
        scenario.on_command(cmd)

Outcomes (any thread):
    bus.ack(cmd_id, target=..., latency_ms=0)
    bus.reject(cmd_id, reason="...", target=...)

Outcome emits to BOTH the frontend `/` and the agent `/agent` namespaces so
the frontend's CommandPanel sees the same ack/reject the Agent does (spec
§7.1.2 + §7.2). Calls are non-blocking — they schedule `sio.emit` on the
provided asyncio loop via `call_soon_threadsafe` so the tick thread never
awaits on I/O.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import time
from typing import TYPE_CHECKING, Iterator

from carlabridge.commands.enum import ParsedCommand

if TYPE_CHECKING:  # pragma: no cover
    import socketio

    from carlabridge.obs.event_log import EventLog

log = logging.getLogger(__name__)


class CommandBus:
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        sio: "socketio.AsyncServer",
        event_log: "EventLog",
        maxsize: int = 64,
    ) -> None:
        self._loop = loop
        self._sio = sio
        self._event_log = event_log
        self._q: queue.Queue[ParsedCommand] = queue.Queue(maxsize=maxsize)
        self._submitted_at: dict[str, float] = {}

    # ---- producer / consumer ------------------------------------------

    def submit(self, cmd: ParsedCommand) -> bool:
        """Non-blocking. Returns True if queued; False if the queue is full
        (caller should emit `agent_reject`)."""
        try:
            self._q.put_nowait(cmd)
        except queue.Full:
            log.warning("command queue full; dropping %s", cmd.id)
            return False
        self._submitted_at[cmd.id] = time.monotonic()
        return True

    def drain(self) -> Iterator[ParsedCommand]:
        """Yield every queued command without blocking. Tick thread uses this."""
        while True:
            try:
                yield self._q.get_nowait()
            except queue.Empty:
                return

    def depth(self) -> int:
        return self._q.qsize()

    # ---- outcomes (called from any thread) ----------------------------

    def ack(
        self, cmd_id: str, *, target: str | None = None, latency_ms: int | None = None
    ) -> None:
        if latency_ms is None:
            t0 = self._submitted_at.pop(cmd_id, None)
            latency_ms = int((time.monotonic() - t0) * 1000) if t0 is not None else 0
        else:
            self._submitted_at.pop(cmd_id, None)
        payload = {"id": cmd_id, "target": target, "latency_ms": latency_ms}
        self._fan_out("agent_ack", payload)
        self._event_log.add(
            "ok", "SCENARIO", f"ack {cmd_id} target={target} latency={latency_ms}ms",
        )

    def reject(
        self, cmd_id: str, *, reason: str, target: str | None = None
    ) -> None:
        self._submitted_at.pop(cmd_id, None)
        payload = {"id": cmd_id, "target": target, "reason": reason}
        self._fan_out("agent_reject", payload)
        self._event_log.add(
            "warn", "SCENARIO", f"reject {cmd_id} target={target} reason={reason}",
        )

    # ---- internals ----------------------------------------------------

    def _fan_out(self, event: str, payload: dict) -> None:
        """Schedule emit on the asyncio loop from any thread.

        Emits to both namespaces — both the frontend (so the CommandPanel can
        flag the command) and the Agent (so it sees its own command echoed).
        """
        for namespace in ("/", "/agent"):
            try:
                self._loop.call_soon_threadsafe(
                    self._sio.start_background_task,
                    self._emit_async,
                    event,
                    payload,
                    namespace,
                )
            except RuntimeError:
                # Loop already closed during shutdown — drop the emit.
                pass

    async def _emit_async(self, event: str, payload: dict, namespace: str) -> None:
        try:
            await self._sio.emit(event, payload, namespace=namespace)
        except Exception:  # pragma: no cover -- best-effort
            log.exception("emit %s to %s failed", event, namespace)


__all__ = ["CommandBus"]
