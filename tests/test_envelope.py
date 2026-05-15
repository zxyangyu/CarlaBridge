"""Protocol v1.0 envelope helpers (bridge-agent-protocol-v1.md §3.1, §3.2)."""

from __future__ import annotations

from carlabridge.bus.envelope import PROTOCOL_VERSION, unwrap, wrap


def test_wrap_has_all_protocol_fields():
    env = wrap("state_snapshot", {"sim_time": 1.0}, sim_time=1.0, frame=42)
    assert env["version"] == "1.0"
    assert env["type"] == "state_snapshot"
    assert env["sender"] == "bridge"
    assert env["sim_time"] == 1.0
    assert env["frame"] == 42
    assert env["payload"] == {"sim_time": 1.0}
    assert isinstance(env["msg_id"], str) and len(env["msg_id"]) >= 8
    assert isinstance(env["timestamp"], float)


def test_wrap_defaults_optional_fields_to_none():
    env = wrap("event_log", {"severity": "info", "source": "BRIDGE", "message": "x"})
    assert env["sim_time"] is None
    assert env["frame"] is None
    assert env["sender"] == "bridge"


def test_wrap_sender_override():
    env = wrap("event_log", {"message": "x"}, sender="agent")
    assert env["sender"] == "agent"


def test_wrap_msg_id_unique_per_call():
    a = wrap("event_log", {})
    b = wrap("event_log", {})
    assert a["msg_id"] != b["msg_id"]


def test_unwrap_envelope_returns_inner_payload():
    env = wrap("command_status", {"cmd_id": "c-1", "status": "completed"})
    inner = unwrap(env)
    assert inner == {"cmd_id": "c-1", "status": "completed"}


def test_unwrap_bare_dict_returned_as_is():
    bare = {"cmd_id": "c-1", "status": "completed"}
    assert unwrap(bare) is bare


def test_unwrap_handles_none():
    assert unwrap(None) == {}


def test_unwrap_handles_non_dict():
    assert unwrap("not a dict") == {}
    assert unwrap(42) == {}


def test_unwrap_envelope_with_non_dict_payload_returns_outer():
    """If "payload" is not a dict, fall back to the outer dict (defensive)."""
    weird = {"version": "1.0", "payload": "scalar"}
    assert unwrap(weird) is weird


def test_protocol_version_constant():
    assert PROTOCOL_VERSION == "1.0"
