"""Unit tests for SimClock."""

from __future__ import annotations

import time

import pytest

from carlabridge.core.clock import SimClock


def test_initial_state():
    c = SimClock(delta=0.0333)
    assert c.sim_time == 0.0
    assert c.tick_count == 0
    assert c.delta == 0.0333


def test_advance_accumulates():
    c = SimClock(delta=0.05)
    for i in range(10):
        c.advance()
    assert c.tick_count == 10
    assert c.sim_time == pytest.approx(0.5)


def test_start_resets():
    c = SimClock(delta=0.05)
    c.advance()
    c.advance()
    assert c.tick_count == 2
    c.start()
    assert c.sim_time == 0.0
    assert c.tick_count == 0


def test_wall_elapsed_progresses():
    c = SimClock(delta=0.01)
    t0 = c.wall_elapsed
    time.sleep(0.05)
    t1 = c.wall_elapsed
    assert t1 - t0 >= 0.04


def test_invalid_delta():
    with pytest.raises(ValueError):
        SimClock(delta=0)
    with pytest.raises(ValueError):
        SimClock(delta=-0.1)
