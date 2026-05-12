"""Scenario package. Importing this module triggers registry registration
for every concrete scenario shipped with the bridge."""

from __future__ import annotations

from carlabridge.scenarios.base import (
    Scenario,
    available_scenarios,
    get_scenario_class,
    register_scenario,
)

# Side-effect import: registers @register_scenario("s1_fire") on import.
from carlabridge.scenarios import s1_fire  # noqa: F401

__all__ = [
    "Scenario",
    "register_scenario",
    "get_scenario_class",
    "available_scenarios",
]
