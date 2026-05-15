"""CommandBus — submit / drain / depth + command_status callback hook (R1)."""

from __future__ import annotations

import asyncio

from carlabridge.commands.bus import CommandBus
from carlabridge.commands.enum import CommandKind, ParsedCommand
from carlabridge.obs.event_log import EventLog


def _mk_cmd(idx: int = 0) -> ParsedCommand:
    return ParsedCommand(
        id=f"c{idx}", kind=CommandKind.UAV_HOLD, target="UAV-01",
    )


def _make_bus(maxsize: int = 4) -> CommandBus:
    loop = asyncio.new_event_loop()
    try:
        evlog = EventLog(capacity=200)
        return CommandBus(loop=loop, sio=None, event_log=evlog, maxsize=maxsize)
    finally:
        loop.close()


# ---- submit / drain -------------------------------------------------------


def test_submit_returns_true_when_queue_has_room():
    bus = _make_bus(maxsize=3)
    assert bus.submit(_mk_cmd(0)) is True
    assert bus.depth() == 1


def test_submit_and_drain_preserves_fifo_order():
    bus = _make_bus(maxsize=4)
    for i in range(3):
        assert bus.submit(_mk_cmd(i)) is True
    drained = list(bus.drain())
    assert [c.id for c in drained] == ["c0", "c1", "c2"]
    assert bus.depth() == 0


def test_drain_is_empty_after_full_drain():
    bus = _make_bus(maxsize=3)
    bus.submit(_mk_cmd(0))
    list(bus.drain())
    assert list(bus.drain()) == []


def test_submit_returns_false_when_queue_is_full():
    bus = _make_bus(maxsize=2)
    assert bus.submit(_mk_cmd(0)) is True
    assert bus.submit(_mk_cmd(1)) is True
    assert bus.submit(_mk_cmd(2)) is False
    # Full submit must not stash anything else.
    assert bus.depth() == 2


def test_submit_records_timestamp_for_acknowledge():
    bus = _make_bus(maxsize=2)
    bus.submit(_mk_cmd(0))
    assert bus.submitted_at("c0") is not None


def test_submit_full_does_not_record_timestamp():
    bus = _make_bus(maxsize=1)
    bus.submit(_mk_cmd(0))
    bus.submit(_mk_cmd(1))  # rejected
    assert bus.submitted_at("c1") is None


def test_forget_removes_timestamp():
    bus = _make_bus(maxsize=2)
    bus.submit(_mk_cmd(0))
    assert bus.submitted_at("c0") is not None
    bus.forget("c0")
    assert bus.submitted_at("c0") is None


def test_forget_unknown_id_is_safe_noop():
    bus = _make_bus(maxsize=2)
    bus.forget("no-such-id")  # must not raise


# ---- command_status callback (placeholder wiring for R5) -------------------


def test_broadcast_command_status_without_callback_is_noop():
    bus = _make_bus(maxsize=2)
    # No callback set → call must succeed silently.
    bus.broadcast_command_status({"cmd_id": "c0", "status": "completed"})


def test_broadcast_command_status_invokes_callback():
    bus = _make_bus(maxsize=2)
    seen: list[dict] = []
    bus.set_on_command_status(seen.append)
    bus.broadcast_command_status({"cmd_id": "c0", "status": "completed"})
    bus.broadcast_command_status({"cmd_id": "c1", "status": "cancelled", "reason": "superseded"})
    assert [p["cmd_id"] for p in seen] == ["c0", "c1"]
    assert seen[1]["reason"] == "superseded"


def test_broadcast_command_status_swallows_callback_errors():
    bus = _make_bus(maxsize=2)

    def boom(_p: dict) -> None:
        raise RuntimeError("boom")

    bus.set_on_command_status(boom)
    # Must NOT raise — broadcasting is best-effort and never blocks the sim
    # tick on a downstream socket failure.
    bus.broadcast_command_status({"cmd_id": "c0", "status": "failed"})


def test_set_on_command_status_can_be_cleared():
    bus = _make_bus(maxsize=2)
    seen: list[dict] = []
    bus.set_on_command_status(seen.append)
    bus.broadcast_command_status({"cmd_id": "c0", "status": "completed"})
    bus.set_on_command_status(None)
    bus.broadcast_command_status({"cmd_id": "c1", "status": "completed"})
    assert [p["cmd_id"] for p in seen] == ["c0"]
