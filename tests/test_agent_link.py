"""MockAgentLink + SocketIOAgentLink behavior."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from carlabridge.agent.mock_agent import MockAgentLink
from carlabridge.agent.socketio_agent import SocketIOAgentLink
from carlabridge.commands.bus import CommandBus
from carlabridge.obs.event_log import EventLog


class FakeSio:
    def __init__(self) -> None:
        self.emits: dict[tuple[str, str], list[dict]] = defaultdict(list)

    def start_background_task(self, coro_fn, *args, **kwargs):
        return asyncio.ensure_future(coro_fn(*args, **kwargs))

    async def emit(self, event: str, payload: dict, namespace: str = "/", **_) -> None:
        self.emits[(event, namespace)].append(payload)


def _make() -> tuple[MockAgentLink, CommandBus, EventLog, FakeSio]:
    loop = asyncio.get_event_loop()
    sio = FakeSio()
    evlog = EventLog(capacity=200)
    bus = CommandBus(loop=loop, sio=sio, event_log=evlog, maxsize=4)
    link = MockAgentLink(command_bus=bus, event_log=evlog)
    return link, bus, evlog, sio


async def test_mock_emit_command_parses_and_queues():
    link, bus, _, _ = _make()
    await link.emit_command(
        {"id": "m1", "target": "UAV-02", "text": "UAV_RTL", "priority": "high"}
    )
    drained = list(bus.drain())
    assert len(drained) == 1
    assert drained[0].id == "m1"


async def test_mock_emit_command_bad_payload_rejects():
    link, bus, _, sio = _make()
    await link.emit_command(
        {"id": "m2", "target": "UAV-02", "text": "MYSTERY"}
    )
    # Nothing queued.
    assert list(bus.drain()) == []
    # A reject was scheduled.
    await asyncio.sleep(0.05)
    assert sio.emits[("agent_reject", "/")][0]["id"] == "m2"


async def test_mock_emit_command_overload_rejects():
    link, bus, _, sio = _make()
    # Fill the bus (maxsize=4).
    for i in range(4):
        await link.emit_command(
            {"id": f"f{i}", "target": "UAV-01", "text": "UAV_HOLD"}
        )
    # 5th overflows → reject.
    await link.emit_command(
        {"id": "overflow", "target": "UAV-01", "text": "UAV_HOLD"}
    )
    await asyncio.sleep(0.05)
    rejects = sio.emits[("agent_reject", "/")]
    assert any(r["id"] == "overflow" and r["reason"] == "overloaded" for r in rejects)


async def test_mock_emit_event_log_writes_to_eventlog():
    link, _, evlog, _ = _make()
    await link.emit_event_log("warn", "AGENT", "test message")
    msgs = [e.message for e in evlog.recent()]
    assert "test message" in msgs


async def test_mock_on_suggestion_rejects_by_default():
    """T-M6-12: mock mode default policy is to reject frontend suggestions."""
    link, _, _, sio = _make()
    await link.on_suggestion(
        {"id": "s1", "target": "UAV-02", "text": "UAV_RTL", "priority": "high"}
    )
    await asyncio.sleep(0.05)
    rejects = sio.emits[("agent_reject", "/")]
    assert any(r["id"] == "s1" for r in rejects)


# ---------- SocketIOAgentLink ---------------------------------------------


async def test_socketio_link_emits_suggestion_with_source_tag():
    sio = FakeSio()
    evlog = EventLog(capacity=50)
    link = SocketIOAgentLink(sio=sio, event_log=evlog)
    await link.on_suggestion({"id": "s2", "target": "UAV-01", "text": "UAV_RTL"})
    payloads = sio.emits[("suggestion", "/agent")]
    assert len(payloads) == 1
    assert payloads[0]["source"] == "FRONTEND"
    assert payloads[0]["id"] == "s2"


async def test_socketio_link_event_log_dual_writes():
    """Event log goes to both the local buffer AND the /agent namespace."""
    sio = FakeSio()
    evlog = EventLog(capacity=50)
    link = SocketIOAgentLink(sio=sio, event_log=evlog)
    await link.emit_event_log("warn", "AGENT", "remote agent saw fire")
    assert any(e.message == "remote agent saw fire" for e in evlog.recent())
    assert sio.emits[("event_log", "/agent")][0]["severity"] == "warn"
