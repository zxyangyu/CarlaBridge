"""Incident — fleet-tracked active event (design §4.1, §6.1).

No ``status`` field by design: an Incident's presence in
``fleet.incidents`` is itself the "active" signal. Removal means the event
is handled (Agent infers ``fire_extinguished`` from disappearance).

Spawned by ``S1FireScenario.ignite_fire`` (R4) in response to
``POST /scenario/fire``; removed by ``_handle_ugv_extinguish`` (R4) when
the fire actor is destroyed.
"""

from __future__ import annotations

from dataclasses import dataclass

from carlabridge.core.fleet import Pose


@dataclass(slots=True)
class Incident:
    id: str
    kind: str           # e.g. "fire"
    position: Pose
    severity: str       # e.g. "low" | "medium" | "high"
    since_sim_time: float

    def to_wire(self) -> dict:
        """Serialise for ``state.snapshot.payload.incidents[]`` (design §4.1)."""
        return {
            "id": self.id,
            "kind": self.kind,
            "position": {
                "x": self.position.x,
                "y": self.position.y,
                "z": self.position.z,
            },
            "severity": self.severity,
            "since_sim_time": self.since_sim_time,
        }


__all__ = ["Incident"]
