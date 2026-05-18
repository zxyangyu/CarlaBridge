"""Broadcaster integration: hand a recording fake SIO, verify periodic emits."""

from __future__ import annotations

import asyncio
from collections import defaultdict

import pytest

from carlabridge.bus.broadcaster import Broadcaster
from carlabridge.bus.projector import FocusBinding
from carlabridge.core.atomic import AtomicRef
from carlabridge.core.snapshot import UavState, VehicleState, WorldSnapshot
from carlabridge.obs.metrics import Metrics


class FakeSio:
    def __init__(self) -> None:
        self.emits: dict[tuple[str, str], list[dict]] = defaultdict(list)

    async def emit(self, event: str, payload: dict, namespace: str = "/", **_) -> None:
        self.emits[(event, namespace)].append(payload)


def _snap_with_one_each() -> WorldSnapshot:
    return WorldSnapshot(
        sim_time=0.5,
        vehicles=[
            VehicleState(
                id="UGV-01",
                role="dispatchable",
                pose=(1, 2, 0),
                yaw=0,
                speed=2.0,
                heading=0,
            )
        ],
        uavs=[
            UavState(
                id="UAV-01",
                role="patrol",
                pose=(0, 0, 50),
                altitude=50,
                heading=0,
                battery=80,
            )
        ],
    )


@pytest.mark.asyncio
async def test_broadcaster_emits_state_periodically():
    sio = FakeSio()
    ref: AtomicRef[WorldSnapshot] = AtomicRef(_snap_with_one_each())
    bc = Broadcaster(
        sio=sio,
        snapshot_ref=ref,
        focus=FocusBinding(),
        metrics=Metrics(),
        state_hz=50.0,  # crank up so we see emits inside 250ms
        metrics_hz=10.0,
    )
    bc.start()
    await asyncio.sleep(0.25)
    await bc.stop()

    fe_emits = sio.emits[("state_update", "/")]
    ag_emits = sio.emits[("state_snapshot", "/agent")]
    sm_emits = sio.emits[("system_metrics", "/")]
    # 50 Hz * 0.25s ~= 12, allow loose bounds (CI jitter)
    assert len(fe_emits) >= 5
    assert len(ag_emits) >= 5
    assert len(sm_emits) >= 1

    fe_payload = fe_emits[0]
    assert "city" in fe_payload
    assert fe_payload["uav"]["id"] == "UAV-01"
    assert fe_payload["ugv"]["id"] == "UGV-01"

    # Protocol v1.0 §3.1: /agent emits are envelope-wrapped.
    ag_envelope = ag_emits[0]
    assert ag_envelope["version"] == "1.0"
    assert ag_envelope["type"] == "state_snapshot"
    assert ag_envelope["sender"] == "bridge"
    ag_payload = ag_envelope["payload"]
    assert ag_payload["sim_time"] == 0.5
    assert len(ag_payload["uavs"]) == 1


@pytest.mark.asyncio
async def test_broadcaster_skips_when_no_snapshot():
    sio = FakeSio()
    ref: AtomicRef[WorldSnapshot] = AtomicRef()  # None
    bc = Broadcaster(
        sio=sio,
        snapshot_ref=ref,
        focus=FocusBinding(),
        metrics=Metrics(),
        state_hz=50.0,
    )
    bc.start()
    await asyncio.sleep(0.1)
    await bc.stop()
    # No snapshot → no state emits.
    assert sio.emits[("state_update", "/")] == []
    assert sio.emits[("state_snapshot", "/agent")] == []


@pytest.mark.asyncio
async def test_broadcaster_metrics_payload_shape():
    sio = FakeSio()
    metrics = Metrics()
    metrics.set("tick_fps", 29.5)
    bc = Broadcaster(
        sio=sio,
        snapshot_ref=AtomicRef(_snap_with_one_each()),
        focus=FocusBinding(),
        metrics=metrics,
        state_hz=50.0,
        metrics_hz=50.0,
    )
    bc.start()
    await asyncio.sleep(0.1)
    await bc.stop()
    sm = sio.emits[("system_metrics", "/")][0]
    assert sm["fps"] == 29.5  # tick_fps surfaces as `fps`
    for key in ("cpu", "gpu", "mem", "net"):
        assert key in sm
        assert 0.0 <= sm[key] <= 100.0
