"""In-process AgentLink — used when `agent.mode = "mock"`.

emit_command parses the wire payload and submits it to the CommandBus, so the
tick thread picks it up via `bus.drain()` and feeds it to `scenario.on_command`.

emit_event_log goes through EventLog (the broadcaster fans it out).

on_suggestion: M6 default = "ignore + reject" (per task T-M6-12). The scenario
or operator may swap in a smarter policy later by subclassing.
"""

from __future__ import annotations

import logging
import uuid

from carlabridge.agent.link import AgentLink
from carlabridge.commands.bus import CommandBus
from carlabridge.commands.dispatcher import parse as parse_command
from carlabridge.commands.enum import RejectCommand
from carlabridge.obs.event_log import EventLog

log = logging.getLogger(__name__)


class MockAgentLink(AgentLink):
    def __init__(self, *, command_bus: CommandBus, event_log: EventLog) -> None:
        self._bus = command_bus
        self._event_log = event_log

    async def emit_command(self, cmd: dict) -> None:
        try:
            parsed = parse_command(cmd)
        except RejectCommand as r:
            self._bus.reject(
                cmd.get("id", "?"),
                target=cmd.get("target"),
                reason=f"mock-parse-error: {r}",
            )
            return
        ok = self._bus.submit(parsed)
        if not ok:
            self._bus.reject(parsed.id, target=parsed.target, reason="overloaded")

    async def emit_event_log(
        self, severity: str, source: str, message: str
    ) -> None:
        self._event_log.add(severity, source, message)  # type: ignore[arg-type]

    async def on_suggestion(self, payload: dict) -> None:
        # Default policy: do NOT honor frontend suggestions in mock mode.
        # The hook is here so operators can flip behavior in demos without
        # touching the bridge code (subclass + override).
        cmd_id = payload.get("id") or f"suggestion-{uuid.uuid4().hex[:8]}"
        self._event_log.add(
            "info", "BRIDGE",
            f"frontend suggestion ignored (mock mode): id={cmd_id} text={payload.get('text')}",
        )
        self._bus.reject(
            cmd_id,
            target=payload.get("target"),
            reason="mock mode, suggestions not honored",
        )


__all__ = ["MockAgentLink"]
