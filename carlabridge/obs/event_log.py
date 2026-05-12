"""Event log — bounded ring buffer of business events.

M7 adds:
- `subscribe(listener)` so a broadcaster can fan new events out to clients
  immediately (instead of only via the 10 Hz state broadcast). Listener is
  called synchronously from the producer's thread — listener MUST schedule
  any I/O onto an asyncio loop itself.
- `replay-on-connect` is handled by `FrontendNamespace` (it calls `recent()`).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import asdict, dataclass
from threading import RLock
from typing import Callable, Literal

log = logging.getLogger(__name__)

Severity = Literal["info", "ok", "warn", "danger"]
Source = Literal["BRIDGE", "SCENARIO", "AGENT", "CARLA"]

Listener = Callable[["Event"], None]


@dataclass(frozen=True, slots=True)
class Event:
    ts: float
    severity: Severity
    source: Source
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


class EventLog:
    """Thread-safe bounded ring buffer + listener fan-out."""

    def __init__(self, capacity: int = 1000) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._buf: deque[Event] = deque(maxlen=capacity)
        self._lock = RLock()
        self._listeners: list[Listener] = []

    def add(
        self,
        severity: Severity,
        source: Source,
        message: str,
        ts: float | None = None,
    ) -> Event:
        evt = Event(
            ts=ts if ts is not None else time.time(),
            severity=severity,
            source=source,
            message=message,
        )
        with self._lock:
            self._buf.append(evt)
            listeners = list(self._listeners)
        # Fire OUTSIDE the lock so a slow listener can't stall producers.
        for fn in listeners:
            try:
                fn(evt)
            except Exception:  # pragma: no cover -- listener bug shouldn't break add
                log.exception("event_log listener raised")
        return evt

    def recent(self, n: int | None = None) -> list[Event]:
        with self._lock:
            if n is None or n >= len(self._buf):
                return list(self._buf)
            return list(self._buf)[-n:]

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Register a listener. Returns an unsubscribe callable."""
        with self._lock:
            self._listeners.append(listener)

        def _unsubscribe() -> None:
            with self._lock:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass

        return _unsubscribe

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
