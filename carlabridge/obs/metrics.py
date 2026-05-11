"""Metrics — thread-safe key-value container.

M0: storage only. M1 starts populating tick_fps. M2 hooks the broadcaster.
M7 surfaces full payload via /healthz.
"""

from __future__ import annotations

from threading import RLock
from typing import Any


class Metrics:
    """Tiny thread-safe metrics bag.

    Not a time-series; just the latest value per key. Counters use `inc`.
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._lock = RLock()

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def inc(self, key: str, delta: int | float = 1) -> int | float:
        with self._lock:
            new = self._data.get(key, 0) + delta
            self._data[key] = new
            return new

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)
