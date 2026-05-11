"""Latest-wins single-slot frame queue, sim/CARLA-thread → asyncio bridge.

Producer (CARLA sensor callback, runs on CARLA's internal thread):
    fq.set_latest(frame)

Consumer (aiortc track or MJPEG handler, async):
    frame = await fq.get()     # awaits next frame
    frame = fq.try_get()       # non-blocking, returns None if empty

Semantics:
- Single slot — overwriting an unconsumed frame counts a drop.
- `set_latest` is non-blocking and must be safe from any thread.
- `get()` is async; it parks on an `asyncio.Event` bound to the consumer loop.
- The first `set_latest` after construction (or after consumption) wakes the
  consumer at most once per frame.

We deliberately do NOT use asyncio.Queue: that requires the producer to be on
the loop's thread or wrapped in `call_soon_threadsafe`, and its maxsize=1 with
overwrite semantics is awkward. A plain slot + Event matches the contract
without surprises.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)


class FrameQueue:
    def __init__(self, name: str = "frame") -> None:
        self._name = name
        self._slot: Any = None
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None
        # Stats — read-only via properties.
        self._drops = 0
        self._produced = 0
        self._consumed = 0

    # ---- consumer side (async) ---------------------------------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Associate this queue with an asyncio loop.

        Must be called from inside the loop (`asyncio.get_running_loop()`)
        before `set_latest` is invoked from another thread.
        """
        self._loop = loop or asyncio.get_running_loop()
        self._event = asyncio.Event()

    async def get(self) -> Any:
        """Await the next frame. Drops any earlier unconsumed frames."""
        if self._event is None:
            raise RuntimeError("FrameQueue.bind_loop() not called")
        while True:
            await self._event.wait()
            with self._lock:
                frame = self._slot
                self._slot = None
                self._event.clear()
            if frame is not None:
                self._consumed += 1
                return frame
            # Spurious wake (no frame in slot) — loop back.

    def try_get(self) -> Any | None:
        """Non-blocking peek-and-take. Returns None if empty."""
        with self._lock:
            frame = self._slot
            self._slot = None
            if self._event is not None:
                self._event.clear()
            if frame is not None:
                self._consumed += 1
            return frame

    # ---- producer side (any thread) ----------------------------------

    def set_latest(self, frame: Any) -> None:
        """Store the newest frame. Overwrites + counts drop if slot was full."""
        with self._lock:
            if self._slot is not None:
                self._drops += 1
            self._slot = frame
            self._produced += 1
            loop = self._loop
            event = self._event
        if event is None or loop is None:
            return
        # Wake the consumer from any thread.
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            # Loop already closed during shutdown; safe to ignore.
            pass

    # ---- stats -------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def drops(self) -> int:
        return self._drops

    @property
    def produced(self) -> int:
        return self._produced

    @property
    def consumed(self) -> int:
        return self._consumed

    def stats(self) -> dict[str, int]:
        return {"produced": self._produced, "consumed": self._consumed, "drops": self._drops}


__all__ = ["FrameQueue"]
