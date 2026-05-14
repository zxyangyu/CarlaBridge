"""CarlaBridge — realtime middleware between CARLA and frontend / urban agent.

This package's import side-effect adds CARLA's bundled `agents` module to
sys.path so that `from agents.navigation.basic_agent import BasicAgent` works.

The path can be overridden by the `CARLA_AGENTS_ROOT` environment variable.
Default: `D:/carla/PythonAPI/carla`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__version__ = "0.1.0"

_DEFAULT_AGENTS_ROOT = Path("E:/Program Files/CARLA_0.9.16/PythonAPI/carla")
_AGENTS_ROOT = Path(os.environ.get("CARLA_AGENTS_ROOT", _DEFAULT_AGENTS_ROOT))

if _AGENTS_ROOT.is_dir() and str(_AGENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENTS_ROOT))
