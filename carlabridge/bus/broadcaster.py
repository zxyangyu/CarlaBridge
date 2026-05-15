"""Periodic broadcaster — fans out the latest WorldSnapshot to clients.

Lives in the async domain. Two independent paces:
    - state_hz (default 10 Hz): emit `state_update` to `/` and `state_snapshot`
      to `/agent`, projected from the same AtomicRef.
    - metrics_hz (default 1 Hz): emit `system_metrics` to `/`.

`run()` is a cancellable coroutine; pass it to `asyncio.create_task` and call
`task.cancel()` on shutdown. CancelledError is the only expected exit path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from carlabridge.bus.envelope import wrap
from carlabridge.bus.projector import FocusBinding, for_agent, for_frontend
from carlabridge.core.atomic import AtomicRef
from carlabridge.core.snapshot import WorldSnapshot
from carlabridge.obs.event_log import Event, EventLog
from carlabridge.obs.metrics import Metrics

if TYPE_CHECKING:  # pragma: no cover
    import socketio

log = logging.getLogger(__name__)


class Broadcaster:
    def __init__(
        self,
        *,
        sio: "socketio.AsyncServer",
        snapshot_ref: AtomicRef[WorldSnapshot],
        focus: FocusBinding,
        metrics: Metrics,
        event_log: EventLog | None = None,
        state_hz: float = 10.0,
        metrics_hz: float = 1.0,
    ) -> None:
        if state_hz <= 0 or metrics_hz <= 0:
            raise ValueError("hz values must be positive")
        self._sio = sio
        self._snap_ref = snapshot_ref
        self._focus = focus
        self._metrics = metrics
        self._event_log = event_log
        self._state_period = 1.0 / state_hz
        self._metrics_period = 1.0 / metrics_hz
        self._task_state: asyncio.Task | None = None
        self._task_metrics: asyncio.Task | None = None
        self._unsubscribe_event_log: "callable | None" = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- lifecycle ----------------------------------------------------

    def start(self) -> None:
        if self._task_state is not None:
            raise RuntimeError("Broadcaster already started")
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._task_state = loop.create_task(self._state_loop(), name="broadcaster-state")
        self._task_metrics = loop.create_task(self._metrics_loop(), name="broadcaster-metrics")
        # Subscribe to EventLog so new events fan out immediately (rather
        # than waiting up to 100 ms for the next state broadcast tick).
        if self._event_log is not None:
            self._unsubscribe_event_log = self._event_log.subscribe(
                self._on_event_log
            )
        log.info(
            "broadcaster started (state=%.1fHz metrics=%.1fHz event_log=%s)",
            1.0 / self._state_period,
            1.0 / self._metrics_period,
            "subscribed" if self._event_log is not None else "unsubscribed",
        )

    async def stop(self) -> None:
        if self._unsubscribe_event_log is not None:
            self._unsubscribe_event_log()
            self._unsubscribe_event_log = None
        for t in (self._task_state, self._task_metrics):
            if t is None:
                continue
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._task_state = None
        self._task_metrics = None
        log.info("broadcaster stopped")

    # ---- loops --------------------------------------------------------

    async def _state_loop(self) -> None:
        period = self._state_period
        last_emit_ok = True
        while True:
            try:
                await asyncio.sleep(period)
                snap = self._snap_ref.get()
                if snap is None:
                    continue
                fe_payload = for_frontend(snap, self._focus)
                ag_payload = for_agent(snap)
                # Protocol v1.0 §3.1: every event to /agent is wrapped in the
                # canonical envelope. The frontend namespace stays bare — it
                # is out of protocol scope (§1.2) and has its own contract.
                ag_envelope = wrap(
                    "state_snapshot",
                    ag_payload,
                    sim_time=snap.sim_time,
                    frame=getattr(snap, "frame", None),
                )
                # Fan out concurrently; failure of one namespace must not
                # block the other.
                results = await asyncio.gather(
                    self._sio.emit("state_update", fe_payload, namespace="/"),
                    self._sio.emit("state_snapshot", ag_envelope, namespace="/agent"),
                    return_exceptions=True,
                )
                for r in results:
                    if isinstance(r, Exception):
                        if last_emit_ok:
                            log.warning("broadcast emit failed: %s", r)
                        last_emit_ok = False
                        break
                else:
                    last_emit_ok = True
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("state broadcaster iteration crashed")

    # ---- event_log fan-out -------------------------------------------

    def _on_event_log(self, evt: Event) -> None:
        """Listener called from whatever thread fires EventLog.add.

        We must schedule the emit onto the asyncio loop — listeners may be
        invoked from the sim thread, sensor threads, etc.
        """
        if self._loop is None:
            return
        payload = {"severity": evt.severity, "source": evt.source, "message": evt.message}
        try:
            self._loop.call_soon_threadsafe(
                self._sio.start_background_task,
                self._emit_event_async,
                payload,
            )
        except RuntimeError:
            # Loop closing during shutdown — drop the event.
            pass

    async def _emit_event_async(self, payload: dict) -> None:
        # Protocol v1.0 §3.1 / §4.4: agent namespace gets the canonical
        # envelope. The frontend namespace keeps the bare dict — frontend is
        # out of protocol scope (§1.2) and consumes the legacy shape.
        ag_envelope = wrap("event_log", payload)
        try:
            await asyncio.gather(
                self._sio.emit("event_log", payload, namespace="/"),
                self._sio.emit("event_log", ag_envelope, namespace="/agent"),
                return_exceptions=True,
            )
        except Exception:  # pragma: no cover -- best-effort
            log.exception("event_log emit failed")

    async def _metrics_loop(self) -> None:
        period = self._metrics_period
        while True:
            try:
                await asyncio.sleep(period)
                snap_metrics = self._metrics.snapshot()
                payload = {
                    "cpu": snap_metrics.get("cpu", 0),
                    "gpu": snap_metrics.get("gpu", 0),
                    "mem": snap_metrics.get("mem", 0),
                    "net": snap_metrics.get("net", 0),
                    "fps": snap_metrics.get("tick_fps", 0),
                }
                await self._sio.emit("system_metrics", payload, namespace="/")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("metrics broadcaster iteration crashed")


__all__ = ["Broadcaster"]
