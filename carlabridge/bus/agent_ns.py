"""Agent Socket.IO namespace ('/agent').

Refactor v0.3 (design §4.5):

* ``on_agent_command`` is now an **RPC handler** — its return value becomes the
  ``sio.call`` ack on the Agent side. No more ``agent_ack`` / ``agent_reject``
  event emits.
* ``on_hello`` returns ``{server, bridge_session_id, scenario}`` so the Agent
  can detect Bridge restart (session change) vs scenario reset (run_id bump).
* Two broadcast helpers — ``broadcast_command_status`` and
  ``broadcast_scenario_event`` — are async methods invoked from the asyncio
  loop. The sim domain reaches them through CommandBus callbacks that hop
  threads via ``call_soon_threadsafe`` (wired in main).
"""

from __future__ import annotations

import logging
from typing import Callable

import socketio

from carlabridge.bus.envelope import PROTOCOL_VERSION, unwrap, wrap
from carlabridge.bus.projector import for_agent
from carlabridge.commands.bus import CommandBus
from carlabridge.commands.dispatcher import parse as parse_command
from carlabridge.commands.enum import RejectCommand
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
        command_bus: CommandBus | None = None,
        bridge_session_id: str = "",
        scenario_name: str = "",
        sim_time_provider: Callable[[], float] | None = None,
        resetting_provider: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(namespace)
        self._event_log = event_log
        self._snap_ref = snapshot_ref
        self._command_bus = command_bus
        self._bridge_session_id = bridge_session_id
        self._scenario_name = scenario_name
        self._sim_time = sim_time_provider or (lambda: 0.0)
        self._resetting = resetting_provider or (lambda: False)
        self._sids: set[str] = set()

    @property
    def client_count(self) -> int:
        return len(self._sids)

    @property
    def bridge_session_id(self) -> str:
        return self._bridge_session_id

    def set_resetting_provider(self, provider: Callable[[], bool] | None) -> None:
        """Late wiring — the scenario_runner is built after the namespace,
        so main calls this once it exists. ``None`` reverts to "never resetting"."""
        self._resetting = provider or (lambda: False)

    def set_sim_time_provider(self, provider: Callable[[], float] | None) -> None:
        """Same late-wiring pattern for sim_time accessor."""
        self._sim_time = provider or (lambda: 0.0)

    # Design §4.5 wire event names contain dots ("agent.command"); python-
    # socketio's default ``trigger_event`` looks up ``on_<event>`` literally,
    # which yields an invalid Python identifier. Map dots/dashes to
    # underscores before dispatch so wire and handler names stay aligned.
    _EVENT_ALIASES: dict[str, str] = {
        "agent.command": "agent_command",
        "event.log": "event_log",
    }

    async def trigger_event(self, event, *args):  # type: ignore[override]
        normalized = self._EVENT_ALIASES.get(event, event).replace(".", "_").replace("-", "_")
        return await super().trigger_event(normalized, *args)

    # ---- connect / disconnect ----------------------------------------

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> None:
        log.info("agent connected sid=%s", sid)
        self._sids.add(sid)
        self._event_log.add("ok", "BRIDGE", f"agent connected sid={sid}")
        snap = self._snap_ref.get()
        if snap is not None:
            payload = for_agent(snap)
            await self.emit(
                "state_snapshot",
                wrap(
                    "state_snapshot",
                    payload,
                    sim_time=snap.sim_time,
                    frame=getattr(snap, "frame", None),
                ),
                to=sid,
            )

    async def on_disconnect(self, sid: str) -> None:
        log.info("agent disconnected sid=%s", sid)
        self._sids.discard(sid)
        self._event_log.add("info", "BRIDGE", f"agent disconnected sid={sid}")

    # ---- handshake (RPC) ---------------------------------------------

    async def on_hello(self, sid: str, payload: dict | None = None) -> dict:
        """sio.call('hello', {...}) handshake (protocol §2.2).

        Returns the Bridge's identity so the Agent can recognise restart vs
        reset. The ``version`` field is the protocol version (§2.2).
        Tolerates envelope-wrapped or bare inbound payloads (§3.2).
        """
        payload = unwrap(payload) if payload is not None else {}
        log.info("agent hello sid=%s payload=%s", sid, payload)
        self._event_log.add(
            "info", "BRIDGE",
            f"agent hello: {payload.get('agent_id', '?')}",
        )
        return {
            "server": "carlabridge",
            "version": PROTOCOL_VERSION,
            "bridge_session_id": self._bridge_session_id,
            "scenario": self._scenario_name,
        }

    # ---- command RPC --------------------------------------------------

    async def on_agent_command(self, sid: str, payload: dict | None = None) -> dict:
        """sio.call('agent.command', envelope) handler (protocol §5.1).

        Returns:
            {"status": "accepted", "cmd_id": ..., "queued_at_sim_time": ...}
              when the parse and submit succeed.
            {"status": "rejected", "cmd_id": ..., "reason": ..., "detail": ...}
              otherwise. Reasons come from the canonical list (protocol §10.1).

        Per protocol §3.2 the inbound payload may be envelope-wrapped or bare;
        ``unwrap()`` collapses both shapes. The RPC ack is NOT envelope-wrapped
        (protocol §5.1.2 — a simple dict).
        """
        envelope_body = unwrap(payload) if payload is not None else {}
        cmd_id_hint = envelope_body.get("id", "?")

        log.info("agent.command sid=%s id=%s", sid, cmd_id_hint)

        if self._resetting():
            return _rejected(cmd_id_hint, "scenario_resetting")

        if self._command_bus is None:
            return _rejected(
                cmd_id_hint, "internal_error", {"message": "no command_bus wired"},
            )

        try:
            cmd = parse_command(envelope_body)
        except RejectCommand as r:
            self._event_log.add(
                "warn", "AGENT",
                f"reject {cmd_id_hint} reason={r.reason}",
            )
            return _rejected(cmd_id_hint, r.reason, r.detail or None)

        if not self._command_bus.submit(cmd):
            self._event_log.add(
                "warn", "AGENT",
                f"reject {cmd.id} target={cmd.target} reason=overloaded",
            )
            return _rejected(cmd.id, "overloaded")

        self._event_log.add(
            "info", "AGENT",
            f"accept {cmd.id} kind={cmd.kind.value} target={cmd.target}",
            cmd_id=cmd.id,
        )

        return {
            "status": "accepted",
            "cmd_id": cmd.id,
            "queued_at_sim_time": round(self._sim_time(), 3),
        }

    async def on_event_log(self, sid: str, payload: dict) -> None:
        # Pass-through: agent's own decision logs (no return value needed).
        # Per protocol §3.2 + §5.2 the payload may arrive envelope-wrapped or
        # bare; ``unwrap()`` collapses both. ``source`` is always overwritten
        # to AGENT here so spoofed sources can't masquerade as BRIDGE.
        body = unwrap(payload) if payload is not None else {}
        self._event_log.add(
            body.get("severity", "info"),
            "AGENT",
            str(body.get("message", "")),
        )

    # ---- broadcast helpers (async-loop side, called by main wiring) ----

    async def broadcast_command_status(self, payload: dict) -> None:
        """Emit ``command_status`` envelope to every agent (protocol §4.2)."""
        try:
            await self.emit(
                "command_status",
                wrap(
                    "command_status",
                    payload,
                    sim_time=payload.get("at_sim_time"),
                ),
            )
        except Exception:  # pragma: no cover -- best-effort
            log.exception("command_status emit failed")

    async def broadcast_scenario_event(self, payload: dict) -> None:
        """Emit ``scenario_event`` envelope to every agent (protocol §4.3).

        Only carries reset signal in v1.0 — fire ignite / extinguish are
        inferred from ``snapshot.incidents`` diffs.
        """
        try:
            await self.emit(
                "scenario_event",
                wrap(
                    "scenario_event",
                    payload,
                    sim_time=payload.get("at_sim_time"),
                ),
            )
        except Exception:  # pragma: no cover -- best-effort
            log.exception("scenario_event emit failed")


def _rejected(cmd_id: str, reason: str, detail: dict | None = None) -> dict:
    return {
        "status": "rejected",
        "cmd_id": cmd_id,
        "reason": reason,
        "detail": detail,
    }
