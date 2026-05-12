"""Parser + CommandBus tests (no CARLA)."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

import pytest

from carlabridge.commands.bus import CommandBus
from carlabridge.commands.dispatcher import parse
from carlabridge.commands.enum import CommandKind, ParsedCommand, RejectCommand
from carlabridge.obs.event_log import EventLog


# ---------- dispatcher.parse ---------------------------------------------


def test_parse_uav_rtl_minimal():
    cmd = parse({"id": "c1", "target": "UAV-02", "text": "UAV_RTL"})
    assert cmd.id == "c1"
    assert cmd.kind == CommandKind.UAV_RTL
    assert cmd.target == "UAV-02"
    assert cmd.priority == "normal"
    assert cmd.payload == {}


def test_parse_ugv_dispatch_with_latlng():
    cmd = parse({
        "id": "c2", "target": "UGV-01", "text": "UGV_DISPATCH",
        "priority": "high", "payload": {"lat": 31.23, "lng": 121.47},
    })
    assert cmd.kind == CommandKind.UGV_DISPATCH
    assert cmd.priority == "high"
    assert cmd.payload["lat"] == 31.23


def test_parse_ugv_dispatch_missing_payload_rejected():
    with pytest.raises(RejectCommand):
        parse({"id": "c3", "target": "UGV-01", "text": "UGV_DISPATCH"})


def test_parse_unknown_text_rejected():
    with pytest.raises(RejectCommand):
        parse({"id": "c4", "target": "UAV-01", "text": "NUKE_FROM_ORBIT"})


def test_parse_mark_event_allows_empty_target():
    cmd = parse({"id": "c5", "text": "MARK_EVENT", "payload": {"message": "x"}})
    assert cmd.kind == CommandKind.MARK_EVENT
    assert cmd.target == ""


def test_parse_uav_command_requires_target():
    with pytest.raises(RejectCommand):
        parse({"id": "c6", "text": "UAV_RTL"})


def test_parse_non_dict_rejected():
    with pytest.raises(RejectCommand):
        parse("not a dict")  # type: ignore[arg-type]


def test_parse_missing_id_rejected():
    with pytest.raises(RejectCommand):
        parse({"target": "UAV-01", "text": "UAV_RTL"})


# ---------- CommandBus ----------------------------------------------------


class FakeSio:
    def __init__(self) -> None:
        self.emits: dict[tuple[str, str], list[dict]] = defaultdict(list)

    def start_background_task(self, coro_fn, *args, **kwargs):
        # In the real socketio.AsyncServer, start_background_task wraps a
        # coroutine factory and schedules it. We emulate that here by calling
        # the coroutine and scheduling it via the running loop.
        coro = coro_fn(*args, **kwargs)
        return asyncio.ensure_future(coro)

    async def emit(self, event: str, payload: dict, namespace: str = "/", **_) -> None:
        self.emits[(event, namespace)].append(payload)


def _make_bus() -> tuple[CommandBus, FakeSio, EventLog]:
    loop = asyncio.get_event_loop()
    sio = FakeSio()
    evlog = EventLog(capacity=200)
    bus = CommandBus(loop=loop, sio=sio, event_log=evlog, maxsize=3)
    return bus, sio, evlog


async def test_submit_and_drain_in_order():
    bus, _, _ = _make_bus()
    for i in range(3):
        ok = bus.submit(ParsedCommand(id=f"c{i}", kind=CommandKind.UAV_HOLD, target="UAV-01"))
        assert ok
    drained = list(bus.drain())
    assert [c.id for c in drained] == ["c0", "c1", "c2"]
    assert list(bus.drain()) == []  # empty now


async def test_submit_returns_false_on_full():
    bus, _, _ = _make_bus()
    for i in range(3):
        bus.submit(ParsedCommand(id=f"c{i}", kind=CommandKind.UAV_HOLD, target="UAV-01"))
    ok = bus.submit(ParsedCommand(id="overflow", kind=CommandKind.UAV_HOLD, target="UAV-01"))
    assert ok is False


async def test_ack_emits_to_both_namespaces():
    bus, sio, _ = _make_bus()
    bus.submit(ParsedCommand(id="c1", kind=CommandKind.UAV_HOLD, target="UAV-01"))
    bus.ack("c1", target="UAV-01")
    # call_soon_threadsafe schedules; wait a tick for the emits to fire.
    await asyncio.sleep(0.05)
    assert len(sio.emits[("agent_ack", "/")]) == 1
    assert len(sio.emits[("agent_ack", "/agent")]) == 1
    payload = sio.emits[("agent_ack", "/")][0]
    assert payload["id"] == "c1"
    assert payload["target"] == "UAV-01"
    assert isinstance(payload["latency_ms"], int)


async def test_reject_emits_to_both_namespaces():
    bus, sio, _ = _make_bus()
    bus.reject("c9", reason="overloaded", target="UGV-01")
    await asyncio.sleep(0.05)
    assert sio.emits[("agent_reject", "/")][0]["reason"] == "overloaded"
    assert sio.emits[("agent_reject", "/agent")][0]["reason"] == "overloaded"


async def test_latency_measured_from_submit_to_ack():
    bus, sio, _ = _make_bus()
    bus.submit(ParsedCommand(id="cL", kind=CommandKind.UAV_HOLD, target="UAV-01"))
    await asyncio.sleep(0.06)  # at least 60ms delay
    bus.ack("cL", target="UAV-01")
    await asyncio.sleep(0.05)
    payload = sio.emits[("agent_ack", "/")][0]
    assert payload["latency_ms"] >= 50
