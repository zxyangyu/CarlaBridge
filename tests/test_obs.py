"""Smoke tests for obs.event_log and obs.metrics."""

from __future__ import annotations

import threading

import pytest

from carlabridge.obs.event_log import EventLog
from carlabridge.obs.metrics import Metrics


def test_event_log_capacity_truncates():
    log = EventLog(capacity=3)
    for i in range(5):
        log.add("info", "BRIDGE", f"m{i}")
    items = log.recent()
    assert len(items) == 3
    assert [e.message for e in items] == ["m2", "m3", "m4"]


def test_event_log_recent_n():
    log = EventLog(capacity=10)
    for i in range(5):
        log.add("info", "BRIDGE", f"m{i}")
    assert [e.message for e in log.recent(2)] == ["m3", "m4"]
    assert len(log.recent()) == 5


def test_event_log_invalid_capacity():
    with pytest.raises(ValueError):
        EventLog(capacity=0)


def test_event_log_threadsafe_add():
    log = EventLog(capacity=10_000)

    def writer():
        for _ in range(1000):
            log.add("info", "BRIDGE", "x")

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(log) == 4000


def test_metrics_set_get():
    m = Metrics()
    m.set("tick_fps", 29.8)
    assert m.get("tick_fps") == 29.8
    assert m.get("missing", 0) == 0


def test_metrics_inc():
    m = Metrics()
    assert m.inc("dropped_frames") == 1
    assert m.inc("dropped_frames", 5) == 6


def test_metrics_snapshot_is_copy():
    m = Metrics()
    m.set("a", 1)
    snap = m.snapshot()
    snap["a"] = 999
    assert m.get("a") == 1
