"""CommandBus — cross-domain pipe for agent commands.

Producers (async domain)::

    bus.submit(parsed_cmd)            # returns True if queued, False if full

Consumer (sim / tick domain)::

    for cmd in bus.drain():
        scenario.on_command(cmd)

Refactor v0.3 (design §7.1, §7.4): ack / reject are no longer fanned out as
separate Socket.IO events. The ``sio.call`` return value (handled in
``agent_ns``) carries the accept/reject answer. The sim-domain lifecycle
(``completed`` / ``failed`` / ``cancelled`` / ``ongoing``) is broadcast as
``command_status`` events via the optional ``on_command_status`` callback —
wired up in R5.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import time
from typing import TYPE_CHECKING, Callable, Iterator

from carlabridge.commands.enum import ParsedCommand

if TYPE_CHECKING:  # pragma: no cover
    import socketio

    from carlabridge.obs.event_log import EventLog

log = logging.getLogger(__name__)


# Wired in R5 — the sim-domain finalize_command calls this to push a
# `command_status` event onto /agent. Stays None during R1~R4.
CommandStatusCallback = Callable[[dict], None]


class CommandBus:
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        sio: "socketio.AsyncServer | None" = None,
        event_log: "EventLog | None" = None,
        maxsize: int = 64,
        on_command_status: CommandStatusCallback | None = None,
        on_scenario_event: CommandStatusCallback | None = None,
    ) -> None:
        self._loop = loop
        self._sio = sio
        self._event_log = event_log
        self._q: queue.Queue[ParsedCommand] = queue.Queue(maxsize=maxsize)
        self._submitted_at: dict[str, float] = {}
        self._on_command_status = on_command_status
        self._on_scenario_event = on_scenario_event

    # ---- producer / consumer ------------------------------------------

    def submit(self, cmd: ParsedCommand) -> bool:
        """Non-blocking. Returns True if queued; False if the queue is full
        (caller surfaces ``reason="overloaded"`` via sio.call return)."""
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

    def submitted_at(self, cmd_id: str) -> float | None:
        """Monotonic timestamp recorded when ``submit`` accepted ``cmd_id``,
        or None if the bus never saw it."""
        return self._submitted_at.get(cmd_id)

    def forget(self, cmd_id: str) -> None:
        """Drop the submit-timestamp bookkeeping for ``cmd_id`` (called by
        the scenario after finalize)."""
        self._submitted_at.pop(cmd_id, None)

    # ---- lifecycle broadcast (callback wired in R5) -------------------

    def set_on_command_status(self, cb: CommandStatusCallback | None) -> None:
        self._on_command_status = cb

    def broadcast_command_status(self, payload: dict) -> None:
        """Sim-domain hook invoked from ``scenario._finalize_command``. The
        callback set in main (R5) schedules the actual socket emit on the
        async loop; without one, the call is logged-only so unit tests can
        exercise the lifecycle without a live server."""
        cb = self._on_command_status
        if cb is None:
            log.debug(
                "command_status broadcast skipped (no callback): cmd_id=%s status=%s",
                payload.get("cmd_id"), payload.get("status"),
            )
            return
        try:
            cb(payload)
        except Exception:  # pragma: no cover -- best-effort
            log.exception("command_status callback failed")

    # ---- scenario_event (design §4.3) ---------------------------------

    def set_on_scenario_event(self, cb: CommandStatusCallback | None) -> None:
        self._on_scenario_event = cb

    def broadcast_scenario_event(self, payload: dict) -> None:
        """Sim-domain hook for ``scenario_event`` emits. Currently only
        carries the ``reset`` signal (design §4.3)."""
        cb = self._on_scenario_event
        if cb is None:
            log.debug(
                "scenario_event broadcast skipped (no callback): event=%s",
                payload.get("event"),
            )
            return
        try:
            cb(payload)
        except Exception:  # pragma: no cover -- best-effort
            log.exception("scenario_event callback failed")


__all__ = ["CommandBus", "CommandStatusCallback"]
