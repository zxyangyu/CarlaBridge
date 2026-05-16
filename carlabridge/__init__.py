"""CarlaBridge — realtime middleware between CARLA and frontend / urban agent.

Import side-effect: prepends `carlabridge/vendor/` to `sys.path` so that
CARLA's bundled `agents` subpackage (vendored under
`carlabridge/vendor/agents/`) resolves via its original absolute imports
(`from agents.navigation.global_route_planner import GlobalRoutePlanner`).

The vendored copy is shipped with this repo — no external `CARLA_AGENTS_ROOT`
or `sys.path` manipulation against a CARLA install directory is needed.
See `carlabridge/vendor/README.md` for upgrade instructions.
"""

from __future__ import annotations

import sys
from pathlib import Path

__version__ = "0.1.0"

_VENDOR_ROOT = Path(__file__).resolve().parent / "vendor"
_VENDOR_STR = str(_VENDOR_ROOT)
if _VENDOR_ROOT.is_dir() and _VENDOR_STR not in sys.path:
    sys.path.insert(0, _VENDOR_STR)
