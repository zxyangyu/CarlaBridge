"""EventLog subscription + Broadcaster fan-out to / and /agent."""

from __future__ import annotations

import asyncio
from collections import defaultdict

import pytest

from carlabridge.bus.broadcaster import Broadcaster
from carlabridge.bus.projector import FocusBinding
from carlabridge.core.atomic import AtomicRef
from carlabridge.core.snapshot import WorldSnapshot
from carlabridge.obs.event_log import Event, EventLog
from carlabridge.obs.metrics import Metrics


class FakeSio:
    def __init__(self) -> None:
        self.emits: dict[tuple[str, str], list[dict]] = defaultdict(list)

    def start_background_task(self, coro_fn, *args, **kwargs):
        return asyncio.ensure_future(coro_fn(*args, **kwargs))

    async def emit(self, event: str, payload: dict, namespace: str = "/", **_) -> None:
        self.emits[(event, namespace)].append(payload)


# ---------- EventLog.subscribe -------------------------------------------


def test_subscribe_listener_called_synchronously():
    log = EventLog(capacity=50)
    received: list[Event] = []
    log.subscribe(received.append)
    log.add("warn", "AGENT", "hello")
    assert len(received) == 1
    assert received[0].message == "hello"


def test_unsubscribe_callable_removes_listener():
    log = EventLog(capacity=50)
    received: list[Event] = []
    unsub = log.subscribe(received.append)
    log.add("info", "BRIDGE", "first")
    unsub()
    log.add("info", "BRIDGE", "second")
    assert len(received) == 1
    assert received[0].message == "first"


def test_listener_exception_isolated_from_producer():
    log = EventLog(capacity=50)
    others: list[Event] = []

    def crashing(_evt):
        raise RuntimeError("boom")

    log.subscribe(crashing)
    log.subscribe(others.append)
    # Producer must not propagate.
    log.add("warn", "BRIDGE", "ok")
    assert len(others) == 1


# ---------- Broadcaster event_log fan-out --------------------------------


async def test_broadcaster_fans_event_log_to_both_namespaces():
    sio = FakeSio()
    event_log = EventLog(capacity=50)
    bc = Broadcaster(
        sio=sio,
        snapshot_ref=AtomicRef[WorldSnapshot](),
        focus=FocusBinding(),
        metrics=Metrics(),
        event_log=event_log,
        state_hz=50.0,
        metrics_hz=50.0,
    )
    bc.start()
    try:
        # Add an event from this thread (the loop thread) — the listener
        # schedules emit via call_soon_threadsafe.
        event_log.add("warn", "AGENT", "fire detected")
        # Let the scheduled emit run.
        await asyncio.sleep(0.1)
        fe = sio.emits[("event_log", "/")]
        ag = sio.emits[("event_log", "/agent")]
        assert any(p["message"] == "fire detected" for p in fe)
        assert any(p["message"] == "fire detected" for p in ag)
    finally:
        await bc.stop()


async def test_broadcaster_unsubscribes_on_stop():
    sio = FakeSio()
    event_log = EventLog(capacity=50)
    bc = Broadcaster(
        sio=sio,
        snapshot_ref=AtomicRef[WorldSnapshot](),
        focus=FocusBinding(),
        metrics=Metrics(),
        event_log=event_log,
        state_hz=50.0,
    )
    bc.start()
    event_log.add("info", "BRIDGE", "before-stop")
    await asyncio.sleep(0.1)
    await bc.stop()
    # After stop, new events MUST NOT trigger emits.
    pre_count = len(sio.emits[("event_log", "/")])
    event_log.add("info", "BRIDGE", "after-stop")
    await asyncio.sleep(0.1)
    assert len(sio.emits[("event_log", "/")]) == pre_count
