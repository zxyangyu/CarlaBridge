"""FrameQueue contract: latest-wins, drop counter, threadsafe wake."""

from __future__ import annotations

import asyncio
import threading

import pytest

from carlabridge.sensors.frame_queue import FrameQueue


async def test_async_get_waits_then_returns_set_value():
    fq = FrameQueue("test")
    fq.bind_loop()

    async def producer():
        await asyncio.sleep(0.02)
        fq.set_latest("frame-1")

    asyncio.create_task(producer())
    val = await asyncio.wait_for(fq.get(), timeout=1.0)
    assert val == "frame-1"
    assert fq.consumed == 1
    assert fq.produced == 1
    assert fq.drops == 0


async def test_latest_wins_drops_old_frames():
    fq = FrameQueue("test")
    fq.bind_loop()
    for i in range(5):
        fq.set_latest(f"frame-{i}")
    val = await asyncio.wait_for(fq.get(), timeout=0.5)
    assert val == "frame-4"
    assert fq.produced == 5
    assert fq.drops == 4
    assert fq.consumed == 1


def test_try_get_returns_none_when_empty():
    fq = FrameQueue("test")
    assert fq.try_get() is None


def test_try_get_returns_latest():
    fq = FrameQueue("test")
    fq.set_latest("a")
    fq.set_latest("b")
    assert fq.try_get() == "b"
    assert fq.drops == 1
    assert fq.try_get() is None


async def test_threadsafe_producer_from_other_thread():
    fq = FrameQueue("test")
    fq.bind_loop()
    ready = threading.Event()

    def producer():
        ready.set()
        for i in range(50):
            fq.set_latest(("payload", i))

    t = threading.Thread(target=producer)
    t.start()
    ready.wait()
    # We don't care which i we land on, only that we land at all and counts agree.
    val = await asyncio.wait_for(fq.get(), timeout=1.0)
    t.join(timeout=1.0)
    assert isinstance(val, tuple) and val[0] == "payload"
    assert fq.produced == 50
    assert fq.consumed >= 1
    # Drain any leftover frame, then assert the conservation law:
    #   produced == consumed + drops   (after the slot is empty)
    fq.try_get()  # consumes leftover (if any) and bumps `consumed` accordingly.
    assert fq.drops + fq.consumed == fq.produced


async def test_get_without_bind_loop_raises():
    fq = FrameQueue("test")
    with pytest.raises(RuntimeError):
        await fq.get()
