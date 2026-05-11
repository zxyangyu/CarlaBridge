"""Atomic single-value reference for sim → async hand-off.

CPython guarantees that single-variable assignment is atomic — no torn reads
between threads — so the simplest possible wrapper is correct. We add a tiny
typed shell to make intent explicit at call sites.

Read pattern (async domain, broadcaster):
    snap = snapshot_ref.get()
    if snap is not None:
        emit(...)

Write pattern (sim domain, tick loop):
    snapshot_ref.set(builder.build(...))
"""

from __future__ import annotations

from typing import Generic, TypeVar

T = TypeVar("T")


class AtomicRef(Generic[T]):
    __slots__ = ("_value",)

    def __init__(self, initial: T | None = None) -> None:
        self._value: T | None = initial

    def set(self, value: T) -> None:
        self._value = value

    def get(self) -> T | None:
        return self._value


__all__ = ["AtomicRef"]
