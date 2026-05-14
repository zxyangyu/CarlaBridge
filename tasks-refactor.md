# CarlaBridge 重构任务（tasks-refactor.md）

> 文档目的：把 `design-refactor-agent-boundary.md` v0.3 拆成可执行、可验收、可排期的任务清单。
> 上游：`design-refactor-agent-boundary.md` v0.3（已评审 §15 通过）。
> 配套：原有 M0~M8 见 `tasks.md`；本文档只列**重构 R 系列**任务。
> 状态：所有任务初始为 `todo`，按 R 顺序推进；同 R 内的任务允许并行（依赖已标注）。

---

## 0. 任务约定（与 tasks.md 一致）

| 项 | 值 |
|---|---|
| 任务 ID | `T-R{N}-{nn}`（N = 重构步骤 1~10） |
| 估时口径 | `S` ≤ 0.5 day / `M` 0.5~1.5 day / `L` 1.5~3 day / `XL` > 3 day |
| 状态 | `todo` → `wip` → `blocked` / `done`（本文不跟踪状态，仅给 DoD） |
| 工程目录 | `D:\Urban_v2\CarlaBridge` |
| 测试运行 | `pytest -q`（conda env `D:/carla/env`） |

**通用 DoD**（每条任务隐含）：
- `pytest -q` 全绿（不含 CARLA 联跑用例除非显式声明）
- `python -m carlabridge.main` 能正常启动到 listen 状态
- 无 import 残留、无 dead code（grep 验证）

---

## 1. 重构总览

| R | 范围 | 估时 | 依赖 |
|---|---|---|---|
| R1 | 新命令面骨架（无 CARLA 依赖） | 0.5 day | 无 |
| R2 | 数据模型扩展（Incident / Fleet / Snapshot / Projector） | 0.5 day | R1 |
| R3 | InFlightCommand + 生命周期框架 + supersede | 1 day | R1 |
| R4 | S1FireScenario 重写 | 2 day | R1~R3 |
| R5 | Socket.IO 协议改造（sio.call + 事件广播） | 1 day | R4 |
| R6 | HTTP 控制面（fire / reset / status） | 1 day | R4, R5 |
| R7 | 清理（删 `agent/` 等） | 0.5 day | R4, R5 |
| R8 | 覆盖测试补强 | 1 day | R5, R6 |
| R9 | 测试 agent（`test_agent.py`） | 1 day | R5, R6 |
| R10 | 文档同步 | 0.5 day | R9 |
| **合计** | — | **~9 day** | — |

**关键路径**：R1 → R3 → R4 → R5 → R6 → R9（≈ 6.5 day）。R2 可与 R3 并行；R7/R8/R10 可与上游并行收尾。

---

## 2. R1 — 新命令面骨架

### T-R1-01 改写 `carlabridge/commands/enum.py`
- `CommandKind` 替换为 8 条：`UAV_PATROL / UAV_GOTO / UAV_RTL / UAV_HOLD / UGV_GOTO / UGV_RTL / UGV_EXTINGUISH / UGV_STOP`
- 删除：`MARK_EVENT`、`ATTACH_ACTOR`
- `ParsedCommand`：字段 `text → kind`，`payload → params`
- `RejectCommand` 改造为结构化：`reason: str` + `detail: dict | None`
- **DoD**：`tests/test_commands.py` 替换为 `test_enum_v2.py`，枚举 + RejectCommand 单测通过
- **依赖**：无
- **估时**：S

### T-R1-02 改写 `carlabridge/commands/dispatcher.py`
- 每条命令一个独立 `_validate_<kind>(params)` 函数
- 校验失败抛 `RejectCommand(reason="parse_error", detail={...})`
- 字段语义按设计文档 §3.2 表
- **DoD**：新建 `tests/test_dispatcher_v2.py`，每条命令至少 2 个合法 + 2 个非法 case；reason 枚举覆盖 `parse_error` / `kind_target_mismatch`
- **依赖**：T-R1-01
- **估时**：M

### T-R1-03 改写 `carlabridge/commands/bus.py`
- `submit(cmd)` 保留：投到 sim 队列，返回 bool（true=进队）
- 删除 `reject()` 方法（reject 现在是 sio.call 返回值，不再单独 emit）
- 删除"sim 域执行完毕后 emit agent_ack/agent_reject"的回调路径
- 新增："sim 域执行完毕后调用 `broadcast_command_status` 回调"的占位（R5 接入）
- **DoD**：单元测试 `tests/test_commands_bus.py` 覆盖 submit 满 / submit 成功；旧 reject 测试删
- **依赖**：T-R1-01
- **估时**：S

---

## 3. R2 — 数据模型扩展

### T-R2-01 新增 `carlabridge/core/incident.py`
- `Incident` dataclass：`id / kind / position(Pose) / severity / since_sim_time`
- **不**含 `status` 字段（按 design §4.1）
- 配 `to_wire()` 序列化方法
- **DoD**：import 通过；类型注解 + 单测
- **依赖**：无
- **估时**：S

### T-R2-02 扩展 `Fleet`（`carlabridge/core/fleet.py`）
- 新增字段：`origins: dict[str, Pose]`、`incidents: dict[str, Incident]`
- 方法：`set_origin(eid, pose)` / `get_origin(eid)` / `add_incident(inc)` / `remove_incident(id)` / `clear_incidents()`
- `register()` 默认不写 origin（由 scenario.setup 显式调用）
- **DoD**：`tests/test_fleet.py` 加 6 个 case：set/get origin、add/remove/clear incident、incidents 字典快照
- **依赖**：T-R2-01
- **估时**：S

### T-R2-03 扩展 `WorldSnapshot`
- 新增字段：`run_id: int`、`bridge_session_id: str`、`incidents: list[Incident]`、`in_flight_commands: list[dict]`
- `SnapshotBuilder.build(world, fleet, *, run_id, session_id, in_flight)` 增加新参数
- **DoD**：`tests/test_snapshot.py` 加 case 验证新字段非空 / 默认值正确
- **依赖**：T-R2-02
- **估时**：M

### T-R2-04 扩展 `projector.for_agent`
- 序列化新字段，与 §4.1 payload schema 对齐
- 字段顺序保持稳定（便于 wire diff）
- **DoD**：`tests/test_projector.py` 增加 `test_for_agent_v2` 验证 wire payload；快照测试覆盖 incidents 与 in_flight_commands 排序稳定
- **依赖**：T-R2-03
- **估时**：S

---

## 4. R3 — InFlightCommand + 生命周期框架

### T-R3-01 新增 `InFlightCommand` dataclass
- 字段：`cmd_id / kind / target / params / accepted_at_sim_time / awaiting`
- `awaiting` 取值：`"instant" | "uav_arrival" | "ugv_arrival" | "extinguish" | "patrol_finish" | "ongoing"`
- 放置：`carlabridge/scenarios/in_flight.py`（新文件）或追加到 `commands/enum.py`，前者更清晰
- **DoD**：import 通过；helper `to_snapshot_entry()` 用于 `in_flight_commands` 序列化
- **依赖**：T-R1-01
- **估时**：S

### T-R3-02 `_check_completion` / `_finalize_command` 框架
- 在 `Scenario` 基类（`scenarios/base.py`）加默认空实现 + abstract `_check_completion(in_flt, sim_time) -> CompletionResult | None`
- `CompletionResult` 联合类型：`("completed",) | ("failed", reason, detail) | ("cancelled", reason, detail)`
- `_finalize_command(cmd_id, in_flt, result)`：移出 `_in_flight` / `_in_flight_by_entity`、回调 `broadcast_command_status`（R5 接入前用 stub）+ `event_log.add(... cmd_id=...)`
- **DoD**：`tests/test_command_lifecycle.py` 使用 FakeScenario 验证 instant 命令同 tick completed
- **依赖**：T-R3-01
- **估时**：M

### T-R3-03 Supersede 流程
- 新方法 `_accept_command(cmd, sim_time)`：若 `_in_flight_by_entity[cmd.target]` 存在 → finalize 旧命令为 `cancelled(reason="superseded", detail={by_cmd_id})`；登记新命令
- UGV_STOP 特殊：supersede 时 `reason="explicit_stop"`
- **DoD**：`tests/test_supersede.py`：同 entity 连发 → 旧 cancelled / 新 accepted；STOP supersede → cancelled.reason=explicit_stop
- **依赖**：T-R3-02
- **估时**：M

---

## 5. R4 — S1FireScenario 重写

### T-R4-01 清空旧剧本代码
- 删除：`_SCRIPT` / `ScriptEvent` / `mock_agent_loop` / `_fire_script_event` / `_materialize_payload`
- 删除：`_script_start_sim` / `_sim_time_provider` / `attach_sim_time_provider` / `SCRIPT_TICK_S`
- 删除：`_uav_rtl` / `_uav_hold` / `_ugv_dispatch` / `_ugv_rtl` / `_resolve_ugv_destination`
- 删除：`setup()` 末尾"自动给 UAV-01 set_target"的代码
- **DoD**：`grep -nE "_SCRIPT|mock_agent_loop|ScriptEvent" carlabridge/` 返回 0 行
- **依赖**：T-R3-03
- **估时**：S

### T-R4-02 实现 8 命令 dispatch + 私有处理函数
- `on_command(cmd)` 重写为：调 `_accept_command(cmd)` → 按 `cmd.kind` dispatch 到 `_handle_uav_patrol(cmd)` 等私有方法
- `setup()` 中填充 `fleet.origins`（UGV-01 + UAV-01/02/03）
- 私有方法：
  - `_handle_uav_patrol(cmd)` → `virtual_uav.set_path(path, cruise_speed, loop)`；in_flight.awaiting = `"patrol_finish"` 或 `"ongoing"`
  - `_handle_uav_goto(cmd)` → `virtual_uav.set_target(pose, cruise_speed)`；awaiting = `"uav_arrival"`
  - `_handle_uav_rtl(cmd)` → 同 GOTO 但目标 = `fleet.origins[target]`
  - `_handle_uav_hold(cmd)` → `virtual_uav.set_target(None)`；awaiting = `"instant"`
  - `_handle_ugv_goto(cmd)` → 创建 follower + set_destination；awaiting = `"ugv_arrival"`
  - `_handle_ugv_rtl(cmd)` → 同 GOTO 但目标 = origin
  - `_handle_ugv_extinguish(cmd)` → 距离 check → ok 标记 awaiting = `"extinguish"`；fail 抛 RejectCommand
  - `_handle_ugv_stop(cmd)` → 清 follower + brake；awaiting = `"instant"`
- **DoD**：`tests/test_s1_dispatch.py` 8 条命令各至少 1 个绿 case（用 FakeWorld）
- **依赖**：T-R4-01, T-R3-03
- **估时**：L

### T-R4-03 实现 `ignite_fire(...)` 公共方法
- 签名：`def ignite_fire(self, *, id, position, kind="fire", severity="high", blueprint=None) -> Incident`
- 行为：spawn fire actor（沿用 `_spawn_first_available` 候选 fallback）→ 写入 `_fire_actors[id]` 与 `fleet.incidents[id]`
- 校验：id 重复 → 抛 `ValueError("incident_id already exists")`（HTTP handler 转 409）
- **DoD**：`tests/test_ignite_fire.py`：spawn 后 fleet.incidents 含；重复 id 抛错；blueprint 全失败抛错
- **依赖**：T-R4-02
- **估时**：M

### T-R4-04 实现 `reset()` 公共方法
- 签名：`def reset(self) -> dict`（返回 `{cancelled_commands, destroyed_incidents, new_run_id}`）
- 行为（按 design §5.2）：
  1. 当前所有 `_in_flight` 通过 `_finalize_command(..., cancelled, reason="reset")` 收尾
  2. `teardown()` — destroy 所有 actors + virtual UAVs + fire actors
  3. `setup()` — 重新 spawn 一切；重写 `fleet.origins`
  4. `_run_id += 1`
- **关键**：相机 FrameQueue 实例**必须保留**，仅重绑（已在 `CameraManager.rebind` 支持）
- **DoD**：`tests/test_reset_reinit.py`：reset 前后 carla `actor_id` 不同；entity_id `UGV-01` 稳定；同一个 FrameQueue 对象 id；in_flight 全清；run_id +1
- **依赖**：T-R4-02, T-R4-03
- **估时**：L

### T-R4-05 实现 `on_tick_post(sim_time)` 完整逻辑
- 按 design §6.2：
  1. UAV lerp（per virtual UAV）
  2. UGV follower 推进
  3. 扫 `_in_flight` 每条 → `_check_completion` 分支：
     - UAV_GOTO/RTL：`distance ≤ UAV_ARRIVAL_EPS_M`
     - UGV_GOTO/RTL：follower.done()
     - UAV_PATROL loop=false：path 走完
     - UAV_PATROL loop=true：永远 None
     - UGV_EXTINGUISH (awaiting=extinguish)：本 tick destroy fire actor + remove incident + return completed
     - UAV_HOLD / UGV_STOP (awaiting=instant)：直接 return completed
- **DoD**：`tests/test_command_lifecycle.py` 全 case 通过；`test_no_timed_events.py` 跑 100s sim 无任何自动事件
- **依赖**：T-R4-02
- **估时**：M

---

## 6. R5 — Socket.IO 协议改造

### T-R5-01 `agent_ns.on_agent_command` 改 return-value 形式
- 改签名：`async def on_agent_command(self, sid, payload) -> dict`
- 返回 `{"status": "accepted"|"rejected", "cmd_id": ..., ...}`
- 删除：`agent_ack` / `agent_reject` 旧 emit 路径（命令 bus 回调里也删）
- **DoD**：`tests/test_agent_command_rpc.py`：起服务，用 socketio AsyncClient `await sio.call('agent.command', envelope, namespace='/agent', timeout=2)` 拿到 dict；accepted 与 rejected 各 1 个 case
- **依赖**：T-R4-02
- **估时**：M

### T-R5-02 广播 helpers
- `agent_ns.broadcast_command_status(payload)`：emit `command_status` 到 `/agent` namespace（all sids）
- `agent_ns.broadcast_scenario_event(payload)`：emit `scenario_event` 到 `/agent`
- scenario 通过 `runner.async_broadcast(name, payload)` 跨域调用（`call_soon_threadsafe`）
- **DoD**：`tests/test_broadcast.py`：socketio AsyncClient 订阅 → 收到测试 payload
- **依赖**：T-R5-01
- **估时**：S

### T-R5-03 `on_hello` 返回 `bridge_session_id`
- Bridge main.py 启动时生成 `bridge_session_id = f"br-{uuid4().hex[:8]}"`
- `agent_ns.on_hello(sid, payload) -> dict` 返回 `{"server": "carlabridge", "bridge_session_id": ..., "scenario": "s1_fire"}`
- snapshot 也持续推送 bridge_session_id
- **DoD**：AsyncClient hello → 收到 ack；snapshot 中 session_id 一致
- **依赖**：T-R5-01, T-R2-03
- **估时**：S

### T-R5-04 UAV_PATROL loop=true 的 `ongoing` 事件
- `_accept_command` 后立即对 PATROL loop=true 触发一次 `broadcast_command_status({status:"ongoing"})`
- `_check_completion` 对 loop=true 始终返回 None
- **DoD**：`tests/test_patrol_loop.py`：accept 后 1s 内收到 ongoing；supersede 后收到 cancelled
- **依赖**：T-R5-02, T-R4-05
- **估时**：S

---

## 7. R6 — HTTP 控制面

### T-R6-01 `ScenarioRunner.run_in_sim_domain(fn) -> asyncio.Future`
- 在 `scenarios/runner.py` 新增同步原语：把 callable 投到 sim 域命令队列（作为特殊"内部任务"），sim 域执行完通过 `loop.call_soon_threadsafe(future.set_result, result)` 唤醒 async 域
- 支持异常：sim 域抛错 → `future.set_exception`
- **DoD**：`tests/test_run_in_sim_domain.py`：投递 lambda return 42 → future 解决为 42；投递抛错 → future raises
- **依赖**：无
- **估时**：M

### T-R6-02 `POST /scenario/fire` 路由
- 新文件 `carlabridge/bus/routes_scenario.py`
- 校验 body schema（position 必填）；调 `runner.run_in_sim_domain(scenario.ignite_fire, ...)` 等待
- 错误处理：position 缺失 → 400；id 重复 → 409；spawn 失败 → 409；reset 中 → 503
- **DoD**：`tests/test_routes_scenario.py::test_fire_*`：成功 200；重复 409；位置无效 400
- **依赖**：T-R6-01, T-R4-03
- **估时**：M

### T-R6-03 `POST /scenario/reset` 路由
- 同上文件；调 `runner.run_in_sim_domain(scenario.reset)`；reset 完成后 broadcast scenario_event
- _resetting 标志在 reset 全过程置 True
- **DoD**：`tests/test_routes_scenario.py::test_reset_*`：响应中含 cancelled_commands / destroyed_incidents；scenario_event 被 AsyncClient 收到
- **依赖**：T-R6-01, T-R4-04, T-R5-02
- **估时**：M

### T-R6-04 `GET /scenario/status` 路由
- 同步返回当前 scenario 状态（不进 sim 域，直接读 fleet/in_flight 快照）
- **DoD**：curl 200 + JSON schema 完整（含 entities、in_flight_commands、incidents）
- **依赖**：T-R6-01
- **估时**：S

### T-R6-05 `_resetting` 标志 + 命令 reject `scenario_resetting`
- `agent_ns.on_agent_command` 在 dispatch 前检查 `scenario._resetting`；True → 返回 `{status:"rejected", reason:"scenario_resetting"}`
- HTTP `/scenario/fire` 检查同样：True → 503
- **DoD**：`tests/test_resetting_lockout.py`：mock _resetting=True → sio.call 立即 rejected
- **依赖**：T-R5-01, T-R6-03
- **估时**：S

---

## 8. R7 — 清理

### T-R7-01 删除 `carlabridge/agent/` 整目录
- 删除文件：`link.py` / `mock_agent.py` / `socketio_agent.py` / `__init__.py`
- 全仓 grep 残留：`from carlabridge.agent` / `import carlabridge.agent` 应为 0
- **DoD**：`pytest -q` 全过；`grep -rn "carlabridge.agent" carlabridge/ tests/` 返回 0 行
- **依赖**：T-R4-01, T-R5-01
- **估时**：S

### T-R7-02 删除 `[agent]` 配置与 main.py 分支
- `config/default.toml` 删 `[agent]` 整段
- `carlabridge/config.py` 删 AgentSettings 类
- `carlabridge/main.py` 删 `if cfg.agent.mode == "mock"` 分支与 mock task 启动
- **DoD**：`python -m carlabridge.main` 启动正常；无 KeyError
- **依赖**：T-R7-01
- **估时**：S

### T-R7-03 测试清理
- 删除 `tests/test_agent_link.py`
- 删除 `tests/test_s1_command.py`（已被新 dispatch / lifecycle 测试覆盖）
- 重写 `tests/test_scenarios.py`：删剧本相关，保留 spawn / camera bind 检查
- **DoD**：`pytest --collect-only` 无错；`pytest -q` 全过
- **依赖**：T-R7-01
- **估时**：S

---

## 9. R8 — 覆盖测试补强

### T-R8-01 `tests/test_patrol_loop.py`
- loop=false：path 走完 → completed
- loop=true：accepted 后立即 ongoing；supersede 后 cancelled(superseded)
- **DoD**：2 个 case pass
- **依赖**：T-R5-04
- **估时**：M

### T-R8-02 `tests/test_no_timed_events.py`
- FakeScenario 起服务跑 100s sim_time；断言期间无 ignite_fire / reset / command_status 自动事件
- **DoD**：pass
- **依赖**：T-R4-05
- **估时**：S

### T-R8-03 `tests/test_routes_scenario.py` 完整覆盖
- 三端点正/负 case + reset 时 sio 命令 rejected + fire 重复 id 409 + reset 时 fire 503
- **DoD**：8+ case pass
- **依赖**：T-R6-02 / 03 / 04 / 05
- **估时**：M

### T-R8-04 `tests/test_instant_commands.py`
- HOLD / STOP / EXTINGUISH 在同 tick accepted + completed（sio.call 返 accepted 后 ≤ 1 tick 内收到 command_status:completed）
- **DoD**：3 case pass
- **依赖**：T-R4-05
- **估时**：S

### T-R8-05 `tests/test_reset_cancel_chain.py`
- 起服务 → 发 2 条 UAV_GOTO + 1 条 UGV_GOTO → POST /scenario/reset → 验证收到 3 条 command_status:cancelled(reason="reset")
- **DoD**：pass
- **依赖**：T-R6-03, T-R5-02
- **估时**：M

---

## 10. R9 — 测试 agent（`test_agent.py`）

### T-R9-01 `test_agent.py` 骨架
- 放置：`D:\Urban_v2\CarlaBridge\test_agent.py`（仓 root）
- 单文件、≤ 250 LOC、stdlib + `python-socketio[asyncio_client]`
- 命令行：`argparse` 接受 `--url`、`--namespace`、`--verbose`、`--no-extinguish`
- 启动流程：
  - `sio = socketio.AsyncClient()`；注册 handlers：`state.snapshot` / `command_status` / `scenario_event` / `event_log` / `connect` / `disconnect`
  - `await sio.connect(url, namespaces=[ns])`
  - `await sio.emit('hello', {agent_id:"test-agent"})`
  - 等第一帧 snapshot，记录 bridge_session_id
- **DoD**：`python test_agent.py --url http://127.0.0.1:5000 -v` 跑通连接、收到 snapshot、打印 session_id 后保持
- **依赖**：T-R5-03
- **估时**：M

### T-R9-02 `test_agent.py` 触发逻辑
- 待命：第一帧 snapshot 后给 UAV-01/02/03 各发 UAV_PATROL（path 用 origin 周围 3 个点的循环；`loop=true`）
- 火情响应：
  - 每帧 snapshot 检查 incidents：新 incident_id（之前未见）→ 发 UGV_GOTO（dest = position + (0,3,0) offset）
  - 监视 UGV-01 到 incident 距离 ≤ extinguish_radius（默认 5m）→ 发 UGV_EXTINGUISH
  - 收 command_status:completed (EXTINGUISH) → 发 UGV_RTL
- reset 处理：scenario_event:reset → 清本地 _responding、_dispatched_cmds → 重发 PATROL
- 日志：每收/发 1 个事件打印 1 行
- **关键约束**：
  - 不做决策栈伪装；纯触发→响应映射
  - 不引用 `carlabridge.*` 任何包（仅外部 socketio 协议）
  - 所有阈值/路径在文件顶部常量声明
- **DoD**：端到端冒烟流程（设计文档 §8.1）跑通：
  1. `.\run.ps1`
  2. `python test_agent.py` → 看到 PATROL × 3 已下发
  3. `curl -X POST :5000/scenario/fire -d '{...}'`
  4. test_agent 输出：识别 → UGV_GOTO → 到达 → EXTINGUISH → completed → RTL
  5. `curl -X POST :5000/scenario/reset`
  6. test_agent 输出：reset → 重发 PATROL
- **依赖**：T-R9-01, T-R6-02, T-R6-03
- **估时**：M

---

## 11. R10 — 文档同步

### T-R10-01 更新 `design.md`
- §5.3 控制流：emit + ack/reject → sio.call return-value + command_status 双阶段
- §8 场景引擎：mock 概念取消；剧本概念取消；指向 `design-refactor-agent-boundary.md`
- §9 Mock vs 真实 Agent：整段标注 superseded
- §10 协议与端口：表内加 `/scenario/fire`、`/scenario/reset`、`/scenario/status`
- **DoD**：md preview 可读，所有指向 v0.1 设计的描述都有 superseded 链接
- **依赖**：无
- **估时**：S

### T-R10-02 spec.md 加 D10 决议
- 新增 D10 条目："Agent / Bridge 解耦：8 条执行级命令 + 通用生命周期 + Bridge 时间无关 + UrbanAgent 完全外部 + HTTP 触发 reset/ignite"
- 与 D2 / D3 / D9 交叉链接说明影响
- **DoD**：spec.md 有 D10
- **依赖**：无
- **估时**：S

### T-R10-03 README 更新
- 删 mock-agent / `[agent] mode` 相关步骤
- 加 §"端到端冒烟流程"：3 终端 curl + test_agent 操作（直接抄设计文档 §8.1）
- 加 §"HTTP 控制面"：列三端点 + curl 示例
- **DoD**：按 README 步骤逐字可重现
- **依赖**：T-R9-02
- **估时**：S

### T-R10-04 CLAUDE.md 同步检查
- grep CLAUDE.md 中"mock_agent" / "agent.mode" / "agent_ack" 等关键词 → 全部替换或删除
- **DoD**：grep 残留 0
- **依赖**：无
- **估时**：S

---

## 12. 验收（重构完成定义）

整个重构 **done** 的判定（按顺序验证）：

1. **`pytest -q` 全绿**（含所有新增测试 R1~R8）
2. **`python -m carlabridge.main` 空载启动**：Ctrl+C 干净退出；无 KeyError / ImportError
3. **`grep -rn "mock_agent\|carlabridge.agent\|agent_ack\|agent_reject\|_SCRIPT" carlabridge/ tests/`** 返回 0 行
4. **冒烟流程**（设计 §8.1）：3 终端跑通点火 → 灭火 → reset → 重发 PATROL，无 stuck
5. **CARLA 联跑**：S1 场景下，curl 触发 fire，test_agent 完成处理后 curl reset，循环 5 次稳定
6. **文档同步**：`design.md`、`spec.md`、`README.md`、`CLAUDE.md` 全部无矛盾

---

## 13. 依赖图（关键路径可视化）

```
R1-01 ──► R1-02 ──► R1-03
   │                  │
   ▼                  ▼
R3-01 ──► R3-02 ──► R3-03 ──┐
                            ▼
R2-01 ──► R2-02 ──► R2-03 ──► R2-04
                            │
                            ▼
R4-01 ──► R4-02 ──► R4-03 ──► R4-04
                       │
                       ▼
                    R4-05
                       │
                       ▼
R5-01 ──► R5-02 ──► R5-03 ──► R5-04
   │
   ▼
R6-01 ──► R6-02 ──► R6-05
   │  ──► R6-03
   │  ──► R6-04
   │
   ▼
R7-01 ──► R7-02 ──► R7-03

R8 系列：并行，依赖 R5 / R6 完成

R9-01 ──► R9-02   依赖 R5-03 + R6-02 + R6-03

R10 系列：收尾并行
```

**关键路径**：R1-01 → R1-02 → R3-02 → R3-03 → R4-02 → R4-04 → R5-01 → R6-03 → R9-02

---

## 14. 风险提醒

- **R4-04 reset = teardown + setup** 是最高风险任务：相机重绑、actor_id 变化、`fleet.origins` 重写必须在同一 tick 内原子完成；建议先用 FakeWorld 单测，再 CARLA 联跑
- **R5-01 sio.call return** 依赖 python-socketio 版本；先 `python -c "import socketio; print(socketio.__version__)"` 锁定 ≥ 5.x
- **R9-02 端到端冒烟**会暴露所有边界问题：可能需要回到 R4 / R5 / R6 修小 bug；预留 buffer
