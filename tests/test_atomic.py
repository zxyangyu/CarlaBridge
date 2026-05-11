"""AtomicRef sanity tests — including concurrent set/get."""

from __future__ import annotations

import threading

from carlabridge.core.atomic import AtomicRef


def test_atomic_initial_none():
    ref: AtomicRef[int] = AtomicRef()
    assert ref.get() is None


def test_atomic_initial_value():
    ref = AtomicRef(42)
    assert ref.get() == 42


def test_atomic_set_replaces():
    ref: AtomicRef[str] = AtomicRef("a")
    ref.set("b")
    assert ref.get() == "b"


def test_atomic_concurrent_set_get_no_error():
    """N writer threads + M reader threads; CPython single-assignment is atomic."""
    ref: AtomicRef[int] = AtomicRef(0)
    stop = threading.Event()
    errors: list[Exception] = []

    def writer(start: int):
        i = start
        while not stop.is_set():
            try:
                ref.set(i)
                i += 1
            except Exception as e:  # pragma: no cover -- guard
                errors.append(e)

    def reader():
        while not stop.is_set():
            try:
                _ = ref.get()
            except Exception as e:  # pragma: no cover
                errors.append(e)

    threads = [threading.Thread(target=writer, args=(i * 1000,)) for i in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    threading.Event().wait(0.1)
    stop.set()
    for t in threads:
        t.join(timeout=1.0)

    assert not errors
    # And the final value is a non-None int.
    assert isinstance(ref.get(), int)
