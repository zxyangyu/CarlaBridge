# Vendored third-party code

Python 模块 **`carla`**（与服务端 RPC 的客户端绑定）由仓库根 `pyproject.toml` 从 **PyPI `carla==0.9.16`** 安装，不放在本目录。

## `agents/`

CARLA's `PythonAPI/carla/agents/` subpackage, copied verbatim from
**CARLA 0.9.16**. We use only `agents.navigation.global_route_planner.
GlobalRoutePlanner` (called from `carlabridge/scenarios/waypoint_follower.py`),
but the full subpackage is bundled so future upgrades can be done with a
single `xcopy` over this directory — no edits to the upstream files.

`carlabridge/__init__.py` adds `carlabridge/vendor/` to `sys.path` so the
upstream `from agents.navigation.xxx` absolute imports resolve unchanged.

To upgrade against a newer CARLA release, replace the contents of
`carlabridge/vendor/agents/` with the corresponding `PythonAPI/carla/agents/`
from the new CARLA install.
