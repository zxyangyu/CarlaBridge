"""Event log — bounded ring buffer of business events.

In M0 this is just storage. M2 wires it into Socket.IO broadcaster, and M7
adds replay-on-connect for new clients.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass
from threading import RLock
from typing import Literal

Severity = Literal["info", "ok", "warn", "danger"]
Source = Literal["BRIDGE", "SCENARIO", "AGENT", "CARLA"]


@dataclass(frozen=True, slots=True)
class Event:
    ts: float
    severity: Severity
    source: Source
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


class EventLog:
    """Thread-safe bounded ring buffer.

    `add()` is callable from any thread. `recent()` returns a snapshot list.
    """

    def __init__(self, capacity: int = 1000) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._buf: deque[Event] = deque(maxlen=capacity)
        self._lock = RLock()

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
        return evt

    def recent(self, n: int | None = None) -> list[Event]:
        with self._lock:
            if n is None or n >= len(self._buf):
                return list(self._buf)
            return list(self._buf)[-n:]

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
