"""NF6: downstream slowness must not block the tick thread.

Architectural guarantee: the tick thread writes the latest WorldSnapshot to
an AtomicRef and never awaits the broadcaster. CommandBus uses bounded
non-blocking submit. So even if the broadcaster sleeps or socket.io clients
hang, ticks keep firing at their natural cadence.

This test exercises that contract end-to-end with a FakeWorld:
- spawn TickLoop with snapshot_builder + snapshot_ref
- spawn Broadcaster with a deliberately-slow FakeSio (2 s/emit)
- measure tick_fps over 1.5 s with the slow sink present
- assert the tick continued at full pace
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict

import pytest

from carlabridge.bus.broadcaster import Broadcaster
from carlabridge.bus.projector import FocusBinding
from carlabridge.core.atomic import AtomicRef
from carlabridge.core.clock import SimClock
from carlabridge.core.fleet import Fleet
from carlabridge.core.snapshot import SnapshotBuilder, WorldSnapshot
from carlabridge.core.tick_loop import NoopScenario, TickLoop
from carlabridge.obs.event_log import EventLog
from carlabridge.obs.metrics import Metrics
from tests.fakes.fake_world import FakeWorld


class SlowSio:
    """Stand-in socket.io that takes 2s per emit. If anything awaits its
    emits inline, the test will hang. The broadcaster must NOT propagate this
    slowness back to the tick thread.
    """

    def __init__(self) -> None:
        self.emits: dict[tuple[str, str], int] = defaultdict(int)

    def start_background_task(self, coro_fn, *args, **kwargs):
        return asyncio.ensure_future(coro_fn(*args, **kwargs))

    async def emit(self, event: str, payload: dict, namespace: str = "/", **_) -> None:
        # Deliberately slow.
        await asyncio.sleep(2.0)
        self.emits[(event, namespace)] += 1


async def test_slow_broadcaster_does_not_stall_tick_thread():
    """The tick thread runs on its own OS thread; it never awaits the loop."""
    world = FakeWorld()
    clock = SimClock(delta=1 / 30)
    fleet = Fleet()
    metrics = Metrics()
    event_log = EventLog(capacity=100)
    snapshot_ref: AtomicRef[WorldSnapshot] = AtomicRef()
    builder = SnapshotBuilder(world=world)

    tick_loop = TickLoop(
        world=world,
        clock=clock,
        fleet=fleet,
        scenario=NoopScenario(),
        metrics=metrics,
        event_log=event_log,
        snapshot_builder=builder,
        snapshot_ref=snapshot_ref,
    )

    sio = SlowSio()
    broadcaster = Broadcaster(
        sio=sio,
        snapshot_ref=snapshot_ref,
        focus=FocusBinding(),
        metrics=metrics,
        event_log=event_log,
        state_hz=10.0,
        metrics_hz=1.0,
    )

    tick_loop.start()
    broadcaster.start()
    try:
        # Let it run long enough for tick_fps to stabilize (>= 1s + 0.5s slack).
        await asyncio.sleep(1.6)
        # Broadcaster has issued ~16 emits all of which are sleeping 2s; none
        # have completed yet. Meanwhile tick should be near full rate.
        tick_fps = metrics.get("tick_fps", 0)
    finally:
        tick_loop.stop()
        tick_loop.join(timeout=2.0)
        await broadcaster.stop()

    # NF6: at 30 Hz target with 1ms FakeWorld tick, we expect close to 30 Hz.
    # Slow sio MUST NOT drag this below ~25 Hz.
    assert tick_fps >= 25, (
        f"NF6 fail: slow broadcaster dragged tick to {tick_fps:.1f} Hz "
        f"(should be ~30 Hz unaffected)"
    )


async def test_tick_continues_during_simulated_socketio_hang():
    """Even with NO downstream consumer reading the snapshot, tick advances.

    The AtomicRef just gets overwritten — no backpressure to producer.
    """
    world = FakeWorld()
    clock = SimClock(delta=1 / 60)  # 60 Hz tick for quick test
    fleet = Fleet()
    metrics = Metrics()
    snapshot_ref: AtomicRef[WorldSnapshot] = AtomicRef()
    builder = SnapshotBuilder(world=world)

    tick_loop = TickLoop(
        world=world,
        clock=clock,
        fleet=fleet,
        scenario=NoopScenario(),
        metrics=metrics,
        event_log=EventLog(capacity=10),
        snapshot_builder=builder,
        snapshot_ref=snapshot_ref,
    )
    tick_loop.start()
    try:
        # Producer-only run; nobody reads snapshot_ref. Need >1s for the
        # first tick_fps window to close and write to metrics.
        await asyncio.sleep(1.4)
        tick_fps = metrics.get("tick_fps", 0)
    finally:
        tick_loop.stop()
        tick_loop.join(timeout=2.0)

    assert tick_fps >= 50, (
        f"tick without consumer should be ~60 Hz, got {tick_fps:.1f}"
    )
