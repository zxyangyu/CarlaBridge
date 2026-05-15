"""InFlightCommand — accepted but not yet finalized commands.

Per design §6.1, each entity has at most one in-flight command at a time
(supersede semantics in §6.4). Scenarios keep two indexes:

* ``_in_flight: dict[cmd_id, InFlightCommand]``
* ``_in_flight_by_entity: dict[entity_id, cmd_id]``

``awaiting`` drives the per-tick completion check in
``_check_completion`` (R4):

============================  ==================================================
``awaiting`` value            Resolved by
============================  ==================================================
``"instant"``                 HOLD / STOP — completed within the accepting tick
``"uav_arrival"``             UAV GOTO / RTL — UAV pose within ``UAV_ARRIVAL_EPS``
``"ugv_arrival"``             UGV GOTO / RTL — ``SimpleWaypointFollower.done()``
``"extinguish"``              UGV_EXTINGUISH — after ``EXTINGUISH_DWELL_S`` sim seconds, fire actor destroyed
``"patrol_finish"``           UAV_PATROL loop=false — path walked to end
``"ongoing"``                 UAV_PATROL loop=true — never auto-completes
============================  ==================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from carlabridge.commands.enum import CommandKind

Awaiting = Literal[
    "instant",
    "uav_arrival",
    "ugv_arrival",
    "extinguish",
    "patrol_finish",
    "ongoing",
]


@dataclass(slots=True)
class InFlightCommand:
    cmd_id: str
    kind: CommandKind
    target: str
    params: dict
    accepted_at_sim_time: float
    awaiting: Awaiting
    # Optional 0..1 progress hint surfaced via snapshot for the Agent's
    # reconciliation UX. Long commands (GOTO/RTL/PATROL) update this in
    # ``on_tick_post``; instant ones leave it None.
    progress: float | None = None
    # Free-form scenario-private state (e.g. patrol index). Kept here so
    # supersede / reset can drop it along with the command entry.
    state: dict = field(default_factory=dict)

    def to_snapshot_entry(self) -> dict:
        """Serialise for ``state.snapshot.payload.in_flight_commands[]``
        (design §4.1). Field order is stable to keep wire diffs readable."""
        entry: dict = {
            "cmd_id": self.cmd_id,
            "kind": self.kind.value,
            "target": self.target,
            "accepted_at_sim_time": self.accepted_at_sim_time,
        }
        if self.progress is not None:
            entry["progress"] = self.progress
        return entry


__all__ = ["Awaiting", "InFlightCommand"]
