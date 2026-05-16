# Claude Code 上下文

## 仓库定位
CarlaBridge — 实时中间件，桥接 **CARLA 0.9.16** 仿真器、**React 数字孪生指挥前端**（`D:\urban_frontend`，只读）、**Urban Agent 调度系统**（外部独立进程）。Python 3.12 / Windows 10 / 单进程。

## 入口顺序
读文档/代码请按这个顺序：
1. `README.md` — 安装、启动、HTTP/WS 端点、故障排查
2. `bridge-agent-protocol-v1.md` — Bridge ⇄ Agent 线协议 v1.0（**最高权威**：与任何文档冲突以此为准）
3. `design.md` — 当前架构（HOW，精简版）
4. `carlabridge/` — 实现，目录结构与 `design.md §3` 一一对应
5. `docs/archive/` — 历史 spec/design/tasks（**已 superseded，仅供追溯**，不要据此改代码）

## 平台与命令
- Shell：PowerShell 7；启动器是 Python 脚本 `run.py`（用 `--scenario` 等标准 Python 风格参数）
- Python 环境：仓 root 的 `.\.venv`（conda 前缀环境），不是全局
- 运行测试：`python -m pytest tests/ -q`（145 个，~16 秒，无需 CARLA）
- 启动服务：`python run.py --scenario s1_fire`（README §3）；启动前自动 POST `/admin/shutdown` 优雅释放占用 5000 的旧 bridge（**禁止**改成 kill）
- 端到端冒烟：3 终端 = bridge + `python test_agent.py` + `curl POST /scenario/fire`（README §3.5）

## 不要改动的硬约束
- **协议字段**：envelope `{version, msg_id, type, timestamp, frame, sim_time, sender, payload}` — 见 `carlabridge/bus/envelope.py`，破坏即破坏 Agent 兼容
- **entity_id 稳定**：`UAV-01/02/03` / `UGV-01` 跨 reset 不变；CARLA `actor_id` 可变但 entity_id 是 fleet 注册表的 key
- **每 entity 单条 in-flight 命令**：新命令到 `_accept_command` 必须 supersede 旧命令
- **sim 域不直接 emit**：所有跨域投递走 `loop.call_soon_threadsafe(...)`；违反会有偶发死锁
- **Bridge 完全时间无关**：禁止加 sim_time 触发的剧本/定时器（spec D10），所有 state transition 由命令或 HTTP 触发
- **`/`(前端) namespace 仍裸 payload**：envelope 只用于 `/agent`（避免破坏前端协议）

## 已删除的概念（不要重新引入）
- `carlabridge/agent/` 目录（AgentLink / MockAgentLink / SocketIOAgentLink）
- `agent.mode = "mock" | "remote"` 配置项
- `agent_ack` / `agent_reject` Socket.IO 事件（改成 sio.call return-value）
- `mock_agent_loop` / `SCRIPT` 列表 / `ScriptEvent` / `MARK_EVENT` / `ATTACH_ACTOR` 命令
- 用 CARLA `BasicAgent` 做 UGV 导航（spec D9，永久切到 `SimpleWaypointFollower`）

## 路径漂移提醒
旧文档（archive）里写 `D:\CarlaBridge` + `conda env D:/carla/env`，**实际**在 `E:\Urban_v2\CarlaBridge` + `.\.venv`。如果你看到任何 `D:\CarlaBridge` 路径，那是历史遗留，按新路径执行。

CARLA 的 `agents` 子包（`GlobalRoutePlanner` 等）已 vendored 进 `carlabridge/vendor/agents/`，由 `carlabridge/__init__.py` 加入 `sys.path`。**已删除** `CARLA_AGENTS_ROOT` 环境变量与外部 `PythonAPI/carla` sys.path 注入；如果你看到旧文档里的 `$env:CARLA_AGENTS_ROOT` 或 `D:/carla/PythonAPI/carla`，按 vendored 路径处理。升级 CARLA 时按 `carlabridge/vendor/README.md` 替换该目录。

## 常见任务的去处
- 加新命令 → README §7.3 + protocol §6.4 扩展规约
- 加新 scenario → README §7.2
- 改协议字段 → `bus/envelope.py` + `agent_ns.py` + `broadcaster.py`，并同步 `bridge-agent-protocol-v1.md` + `test_agent.py`
- 验证启停无残留 → `scripts/restart_smoke.ps1`
- 验证内存稳定 → `scripts/nf5_memory_probe.ps1`
