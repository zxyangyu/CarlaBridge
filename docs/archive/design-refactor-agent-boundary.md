> ⚠️ **ARCHIVED — refactor v0.3 + R11 已 100% 落地，保留作为重构决议记录**
>
> 本文是 2026-05-15 评审通过的 Bridge / Agent 边界重构设计。R1~R11 全部完成（详见同目录 `tasks-refactor-r1-r11.md`），代码现状即本文设计。
>
> 当前权威文档：
> - `../../bridge-agent-protocol-v1.md` — 线协议契约（最高优先级）
> - `../../design.md` — 当前架构（精简版，与本文 §6 / §7 等价但简化）
> - `../../README.md` — 入门、运维
>
> 本文档保留价值：
> - **决议出处** — §1 七条目标、§2 角色边界硬约束的决策依据
> - **§7 Bridge 模块改造细则** — 详细列出哪些文件被改/删/新增，便于追溯
> - **§9 数据流示例** — 启动 → 待命 → 火情 → reset 的端到端走查
> - **§10 风险表** — 落地中确实碰到的边界问题
> - **§11 删除清单** — 验证仓库清理完整性的 grep 列表

---

# CarlaBridge × UrbanAgent 重构设计（Agent / Bridge 解耦） — 决议记录

> 文档目的：定义 **WHAT changes & HOW** —— 把 Bridge 内的剧本/策略迁出，固化 Bridge ⇄ Agent 的协议。
> 本文档**仅覆盖 CarlaBridge 仓内**的改动；UrbanAgent 改造由对方仓自行规划（本文档 §8 仅作协议参考）。
> 上游：`design.md`、`spec.md`、`bridge-agent-protocol-v1.md`（线协议契约）
> 下游：`tasks.md` 增量任务（本文档评审通过后追加）

| 字段 | 值 |
|---|---|
| 版本 | refactor v0.3 + R11 envelope 增量 |
| 状态 | R1~R10 已落地，R11(协议 v1.0 envelope 合规)已合入 |
| 决策依据 | 2026-05-15 对话纪要：保留 RTL/HOLD；删 mock-agent；Bridge 时间无关；HTTP 点火；命令通用完成通知；reset = 完全重新初始化；UrbanAgent 与本仓解耦 |

> **R11 增量（2026-05-16 落地）**：所有 `/agent` 出站事件统一包裹协议 v1.0 envelope `{version, msg_id, type, timestamp, frame, sim_time, sender, payload}`（`bridge-agent-protocol-v1.md` §3.1）；`hello` RPC 返回值补 `version: "1.0"` 字段（§2.2）；入站 `on_hello` / `on_agent_command` / `on_event_log` 通过 `bus/envelope.unwrap()` 同时兼容 envelope 与裸 dict 形态（§3.2）。`/`(前端) namespace 出站仍保持裸 payload，前端协议不在 v1.0 范围内（§1.2）。详见 `tasks-refactor.md` §16 R11、`bus/envelope.py`。

---

## 1. 重构目标

### 目标
1. **角色边界硬切**：Bridge 只负责 *HOW*（构建/点火/复位执行、驾驶执行），Agent 只负责 *WHERE/WHEN/WHY*。
2. **Bridge 完全时间无关**：删除任何 sim_time 触发的剧本（自动点火、cooldown 等）。点火、reset 都走 HTTP。
3. **删除 Mock & AgentLink 适配层**：废弃 `agent.mode = "mock"`；删除 `carlabridge/agent/` 整目录。Bridge 永远以"等远程 Agent 接入"模式运行。
4. **命令面 8 条 + 通用生命周期**：每条命令两阶段反馈
   - 阶段 A：sio.call 同步返回 `accepted` / `rejected`
   - 阶段 B：异步 socket 事件 `command_status: completed / failed / cancelled / ongoing`
5. **reset = teardown + setup**：完全销毁所有 actors 后重新创建（不是位置复位）。CARLA actor_id 可能变；entity_id 稳定。
6. **火情走 snapshot 真值 + scenario_event 仅承担 reset**：fire_ignited / fire_extinguished 完全由 snapshot.incidents 的出现/消失推断。
7. **UrbanAgent 完全外部**：Bridge 仓不再含任何 Agent 侧代码、不启动 Agent 进程。

### 非目标
- 不改变摄像头/视频流体系（`design.md` §5.1、§7 完全保留）
- 不改变 tick 线程 / asyncio 双域模型（`design.md` §3）
- 不引入新依赖（python-socketio 已支持 sio.call）
- 不在本期改 UrbanAgent 仓任何代码

---

## 2. 角色分工最终定义

```
┌────────────── UrbanAgent (外部进程，独立仓) ──┐         ┌────────────── CarlaBridge ───────────────┐
│ 决策层                                       │         │ 执行 + 沙盘 actor 控制                     │
│                                              │         │                                          │
│ • 选 UAV 巡逻路径 / 目标点                    │         │ • Spawn UGV / 注册 UAV / 绑相机           │
│ • 选 UGV 目标点 / RTL / HOLD 决策             │         │ • 接收 HTTP 点火 → spawn fire actor       │
│ • 下发灭火动作（UGV_EXTINGUISH）              │         │ • 接收 HTTP reset → teardown + setup       │
│ • 从 snapshot.incidents 感知火情               │         │ • UAV lerp / UGV WaypointFollower 执行    │
│ • 维护命令状态：watch command_status          │         │ • 每 tick 推进 in-flight 命令进度          │
│   + 用 snapshot.in_flight_commands 对账        │         │ • 推 snapshot / command_status / event_log │
│                                              │         │ • 记 fleet.origins（用于 RTL & reset）     │
└──────────────┬─────────────────────────────┘         └────────────────┬─────────────────────────┘
               │                                                        │
               │  state.snapshot (10 Hz, /agent)                         │
               │  ◄──────────────────────────────────────────────────────│
               │   { run_id, bridge_session_id, vehicles, uavs,          │
               │     traffic_lights, incidents, in_flight_commands }     │
               │                                                        │
               │  command_status (event, /agent)                         │
               │  ◄──────────────────────────────────────────────────────│
               │   { cmd_id, status, kind, target, reason?, detail? }    │
               │                                                        │
               │  scenario_event (event, /agent) — only reset            │
               │  ◄──────────────────────────────────────────────────────│
               │   { event:"reset", run_id, trigger:"http" }             │
               │                                                        │
               │  event_log (event, /agent) — human readable             │
               │  ◄──────────────────────────────────────────────────────│
               │   { severity, source, message, cmd_id? }                │
               │                                                        │
               │  agent.command (RPC: sio.call → ack/reject)             │
               │  ──────────────────────────────────────────────────►    │
               │   { id, kind, target, params }                          │
               │                                                        │
       ┌───────┴───────┐                                  ┌──────────────┴────────┐
       │ Operator       │   POST /scenario/fire           │ aiohttp HTTP routes    │
       │ (人 / curl /   │   POST /scenario/reset          │  /scenario/fire        │
       │  cron / GUI)   │   GET  /scenario/status         │  /scenario/reset       │
       │               │ ────────────────────────────►    │  /scenario/status      │
       └───────────────┘                                  └────────────────────────┘
```

**关键约束**：
- Bridge **不主动产生剧情**（无 sim_time 触发、无 cooldown、无状态机）
- 每个 entity 同时只能有 1 条 in-flight 命令；新命令到来 → 旧命令立即 `cancelled(reason="superseded")`
- reset 期间收到的命令一律 reject `reason="scenario_resetting"`
- Agent 不能触发 reset；reset 是 operator 特权

---

## 3. 命令面（8 条命令 + 通用生命周期）

### 3.1 通用 envelope

```json
{
  "version": "1.0",
  "msg_id": "uuid-...",
  "type": "agent.command",
  "timestamp": 1715760000.123,
  "frame": 12345,
  "sim_time": 412.34,
  "sender": "agent",
  "payload": {
    "id": "cmd-9f1",
    "kind": "UAV_GOTO",
    "target": "UAV-01",
    "priority": "normal",
    "params": { ... }
  }
}
```

### 3.2 命令清单（8 条）

| `kind` | target | params | **completed 定义** | 备注 |
|---|---|---|---|---|
| `UAV_PATROL` | UAV id | `path:[{x,y,z}]≥1` + `cruise_speed:float` + `loop:bool=false` | **loop=false**：走完最后一个 waypoint；**loop=true**：永不 completed，仅 `accepted` 后立即 emit `ongoing` 一次 | 下发新 PATROL 直接 supersede |
| `UAV_GOTO` | UAV id | `waypoint:{x,y,z}` + `cruise_speed:float` | UAV 距 waypoint ≤ `UAV_ARRIVAL_EPS`（默认 0.5m） | 到达后 hover 状态 |
| `UAV_RTL` | UAV id | `cruise_speed?:float`（可选，默认配置值） | UAV 距 origin ≤ `UAV_ARRIVAL_EPS` | Bridge 从 `fleet.origins` 取目标 |
| `UAV_HOLD` | UAV id | （无） | 立刻（清 target + path 后同 tick 完成） | 同 tick 内 accepted + completed |
| `UGV_GOTO` | UGV id | `dest:{x,y,z}` + `target_speed?:float`（默认 25 km/h） | `SimpleWaypointFollower.done() == True` | |
| `UGV_RTL` | UGV id | `target_speed?:float` | follower 到达 origin | Bridge 从 `fleet.origins` 取目标 |
| `UGV_EXTINGUISH` | UGV id | `incident_id:str` | fire actor destroyed + incident 移除（下一拍 sim 域完成） | accept 时检查距离阈值 |
| `UGV_STOP` | UGV id | （无） | 立刻（清 follower + apply brake 后同 tick 完成） | 同 tick 内 accepted + completed |

**Bridge 记录 entity origins**：
- `setup()` 中 spawn UGV / 注册 UAV 时填充 `fleet.origins[entity_id] = Pose(...)`
- `*_RTL` 直接读 origins
- reset 后 setup 重新填 origins（spawn point 一般稳定；若变化 Agent 会从下一帧 snapshot 看到新 origin）

### 3.3 ack / reject 返回值（sio.call 同步返回）

```json
// accepted
{ "status": "accepted", "cmd_id": "cmd-9f1", "queued_at_sim_time": 412.34 }

// rejected
{ "status": "rejected", "cmd_id": "cmd-9f1",
  "reason": "not_in_range",
  "detail": { "distance_m": 18.7, "max_m": 5.0 } }
```

`reason` 枚举：

| reason | 触发 |
|---|---|
| `parse_error` | payload schema 错 |
| `unknown_target` | entity_id 不在 fleet |
| `kind_target_mismatch` | UAV_GOTO 但 target 是 UGV，反之 |
| `unknown_incident` | UGV_EXTINGUISH 的 incident_id 不存在 |
| `not_in_range` | UGV 距 incident > `EXTINGUISH_RADIUS`；detail 必带 `distance_m` / `max_m` |
| `no_origin` | *_RTL 时 entity 无 origin（理论上不应发生） |
| `scenario_resetting` | reset 进行中收到任何命令 |
| `overloaded` | command_bus 已满 |
| `internal_error` | 其他异常（detail.message） |

### 3.4 命令生命周期（统一两阶段反馈）

```
                ┌─ rejected ──► (终止；不进 in_flight)
sio.call ──►────┤
                └─ accepted ──► 加入 _in_flight[cmd_id]
                                同时 _in_flight_by_entity[target] = cmd_id
                                │
                                ├─ (instant cmd: HOLD/STOP/EXTINGUISH-after-tick)
                                │    └─► 同 tick / 下一拍 emit command_status:completed
                                │
                                ├─ (long cmd: GOTO/RTL/PATROL loop=false)
                                │    │
                                │    ├─► (期间被新命令替换)
                                │    │     └─► emit command_status:cancelled(reason="superseded")
                                │    │
                                │    ├─► (期间 reset)
                                │    │     └─► emit command_status:cancelled(reason="reset")
                                │    │
                                │    ├─► (期间执行报错，如 follower 崩溃)
                                │    │     └─► emit command_status:failed(reason="follower_error")
                                │    │
                                │    └─► (自然完成)
                                │         └─► emit command_status:completed
                                │
                                └─ (PATROL loop=true)
                                     ├─► accept 后立即 emit command_status:ongoing（声明永不自动完成）
                                     └─► 仅在 supersede / reset 时 emit cancelled
```

**`command_status` 事件 schema**：

```json
{
  "version": "1.0",
  "type": "command_status",
  "frame": 12345,
  "sim_time": 425.10,
  "sender": "bridge",
  "payload": {
    "cmd_id": "cmd-9f1",
    "status": "completed",         // ongoing | completed | failed | cancelled
    "kind": "UGV_GOTO",
    "target": "UGV-01",
    "reason": null,                // null on completed/ongoing；string on failed/cancelled
    "detail": null,                // optional dict
    "at_sim_time": 425.10
  }
}
```

`status="ongoing"`：仅 PATROL loop=true 在 accepted 后立即推一次，明确告知"不会自动 completed"。
`status="cancelled"` 的 reason 枚举：`superseded` / `reset` / `explicit_stop`（被 UGV_STOP 取消）/ `bridge_shutdown`。
`status="failed"` 的 reason 枚举：`follower_error` / `entity_destroyed` / `internal_error`。

### 3.5 双通道分工（command_status × event_log）

按你的决议"两个都发"：

| 事件 | command_status（机器读） | event_log（人读） |
|---|---|---|
| UGV-01 到达终点 | `{cmd_id, status:completed, kind:UGV_GOTO}` | `{severity:ok, source:BRIDGE, message:"UGV-01 arrived at (90, 0, 0)", cmd_id:"cmd-9f1"}` |
| UAV-02 被新命令替换 | `{cmd_id, status:cancelled, reason:superseded}` | `{severity:info, source:BRIDGE, message:"UAV-02 GOTO replaced by RTL", cmd_id:"cmd-9f1"}` |
| follower 崩溃 | `{cmd_id, status:failed, reason:follower_error}` | `{severity:danger, source:BRIDGE, message:"UGV-01 follower crashed; cleared", cmd_id:"cmd-9f1"}` |

**纪律**：
- Agent 决策**只依据** command_status；event_log 是审计/前端展示
- event_log 仍保留**不带 cmd_id** 的环境事件（如 CARLA RPC timeout 警告、`scenario_resetting`）
- 同一条命令的 command_status 与 event_log 会**几乎同时**发出（同一 tick 内 emit）；不保证 socket 帧顺序

### 3.6 旧命令对照

| 旧 | 新位置 |
|---|---|
| `UGV_DISPATCH` | 等价 `UGV_GOTO` |
| `MARK_EVENT` | Agent 通过 `event_log` 通道自己 emit（已存在） |
| `ATTACH_ACTOR` | 本期删除 |

---

## 4. 通道总览

Bridge → Agent 的 4 个推送通道 + Agent → Bridge 的 1 个 RPC 通道，全部在 `/agent` namespace 内：

### 4.1 `state.snapshot`（10 Hz 持续推送）

```json
{
  "version": "1.0",
  "type": "state.snapshot",
  "frame": 12345,
  "sim_time": 12.34,
  "sender": "bridge",
  "payload": {
    "run_id": 5,
    "bridge_session_id": "br-9c1a...",
    "vehicles": [
      {"id": "UGV-01", "position": {"x":..,"y":..,"z":..}, "speed": 5.5, "state": "moving"}
    ],
    "uavs": [
      {"id": "UAV-01", "position": {...}, "battery": 98.2, "state": "cruise"}
    ],
    "traffic_lights": [...],
    "incidents": [
      {"id": "fire-001", "kind": "fire", "position": {...},
       "severity": "high", "since_sim_time": 13.7}
    ],
    "in_flight_commands": [
      {"cmd_id": "cmd-9f1", "kind": "UAV_PATROL", "target": "UAV-01",
       "accepted_at_sim_time": 412.34},
      {"cmd_id": "cmd-a02", "kind": "UGV_GOTO", "target": "UGV-01",
       "accepted_at_sim_time": 418.10, "progress": 0.42}
    ]
  }
}
```

**字段语义**：

| 字段 | 含义 |
|---|---|
| `run_id` | Bridge 启动起的 reset 计数。reset 后 +1。Agent 比对前后值即可识别 reset |
| `bridge_session_id` | Bridge 进程启动 uuid。Agent 重连看到该值变化 = Bridge 重启（与 reset 区分） |
| `incidents[]` | 当前活跃事件；**无 status 字段**——存在即 open，消失即处理完毕 |
| `incidents[].since_sim_time` | incident 创建时的 sim_time，便于 Agent 估算事件年龄 |
| `in_flight_commands[]` | 当前已 accepted 未 completed/failed/cancelled 的命令；Agent 用来对账 |
| `in_flight_commands[].progress` | 可选 0..1，仅 GOTO/RTL/PATROL 类（用 distance ratio）；瞬时命令省略 |

**Agent 使用范式**：
- 主要决策依据：command_status 事件流 + scenario_event
- 重连/丢包兜底：snapshot.in_flight_commands 对账，识别"我发过但没收到完成事件"的命令

### 4.2 `command_status`（事件）

见 §3.4。Bridge 推送时机：
- accepted 之后立即（PATROL loop=true 的 ongoing）
- 命令自然完成（completed）
- 被替换（cancelled superseded）
- reset 时所有 in-flight 一次性 cancelled
- 执行错误（failed）

### 4.3 `scenario_event`（事件）—— **仅 reset**

```json
{
  "version": "1.0",
  "type": "scenario_event",
  "frame": 12345,
  "sim_time": 423.50,
  "sender": "bridge",
  "payload": {
    "event": "reset",
    "run_id": 6,
    "trigger": "http"
  }
}
```

**只承担 reset 信号**；fire_ignited / fire_extinguished 完全靠 snapshot.incidents 出现/消失 + command_status 推断。

Agent 收到 reset → 清空内部决策状态、等下一帧 snapshot 看新 origins / 重发 PATROL。

### 4.4 `event_log`（事件）

保持现有 `{severity, source, message}` schema；**新增可选** `cmd_id`。承担：
- 与命令相关的人类可读叙事（带 cmd_id，与 command_status 配对）
- 非命令相关的环境事件（不带 cmd_id）：CARLA 警告、scenario_resetting 提示等

### 4.5 `agent.command`（RPC）

Agent → Bridge 唯一通道。`await sio.call('agent.command', envelope, namespace='/agent', timeout=…)` 返回 dict（accepted/rejected）。

---

## 5. HTTP 端点（operator 控制面）

新增 aiohttp 路由组 `routes_scenario.py`：

### 5.1 `POST /scenario/fire`

```http
POST /scenario/fire
Content-Type: application/json

{
  "id": "fire-001",                  // 可选；缺省 fire-<uuid8>
  "position": {"x": 90.0, "y": 0.0, "z": 0.0},   // 必填
  "kind": "fire",                    // 可选，默认 "fire"
  "severity": "high",                // 可选，默认 "high"
  "blueprint": "vehicle.carlamotors.firetruck"   // 可选，按候选回退
}
```

**响应**：
```json
// 200 OK
{
  "status": "ok",
  "incident_id": "fire-001",
  "spawned_actor_id": 42,
  "spawned_at_sim_time": 412.34,
  "run_id": 5
}
```

错误码：
- `400` —— position 缺失或类型错
- `409` —— id 重复（已有同 id incident 在 fleet 中）
- `409` —— blueprint 全部 spawn 失败（位置被占用）
- `503` —— reset 进行中（带 retry-after 提示）

### 5.2 `POST /scenario/reset`

```http
POST /scenario/reset
Content-Type: application/json

{}  // 当前无 body 字段
```

**行为**：
1. Bridge 进入 `resetting` 状态（拒绝新命令、新 fire）
2. 把当前所有 _in_flight 命令一次性发 `command_status:cancelled(reason="reset")`
3. 投递工作到 sim 域：
   - `scenario.teardown()` → destroy 所有 actors（UGV、UAV virtual entities、fire actors、cameras）
   - `scenario.setup()` → re-spawn UGV、re-register UAVs、re-spawn cameras（FrameQueue 实例保留 → 视频流不断）、`fleet.origins` 重新写
4. `_run_id += 1`
5. Bridge 退出 `resetting` 状态
6. 广播 `scenario_event{event:"reset", run_id, trigger:"http"}`
7. HTTP handler 返回 200

**响应**：
```json
{
  "status": "ok",
  "run_id": 6,
  "reset_at_sim_time": 423.50,
  "cancelled_commands": ["cmd-9f1", "cmd-a02"],
  "destroyed_incidents": ["fire-001"]
}
```

错误：`503` 如果上一次 reset 还在进行。

### 5.3 `GET /scenario/status`

```json
{
  "name": "s1_fire",
  "run_id": 5,
  "bridge_session_id": "br-9c1a...",
  "sim_time": 412.34,
  "resetting": false,
  "incidents": [...],                // 同 snapshot
  "in_flight_commands": [...],       // 同 snapshot
  "entities": {
    "UGV-01": {"origin": {...}, "current_pose": {...}},
    "UAV-01": {"origin": {...}, "current_pose": {...}}
  }
}
```

无认证（沿用 design.md 非目标）；CORS 复用现有配置。

---

## 6. Bridge 内部职责（无状态机、无定时器）

### 6.1 内部数据结构

```python
class S1FireScenario:
    fleet.origins: dict[str, Pose]             # entity_id → 初始 pose
    fleet.incidents: dict[str, Incident]       # incident_id → Incident
    _fire_actors: dict[str, carla.Actor]       # incident_id → fire actor
    _ugv_follower: SimpleWaypointFollower | None
    _in_flight: dict[str, InFlightCommand]     # cmd_id → in-flight
    _in_flight_by_entity: dict[str, str]       # entity_id → cmd_id (1:1)
    _run_id: int
    _resetting: bool                           # True 期间 reject 新命令
```

```python
@dataclass(slots=True)
class InFlightCommand:
    cmd_id: str
    kind: CommandKind
    target: str
    params: dict
    accepted_at_sim_time: float
    # 状态机字段
    awaiting: str   # "instant" | "uav_arrival" | "ugv_arrival" | "extinguish" | "patrol_finish" | "ongoing"
```

### 6.2 每 tick 处理（`on_tick_post(sim_time)`）

```python
def on_tick_post(self, sim_time):
    if self._resetting:
        return  # nothing during reset

    # 1. UAV lerp
    for uav in self.fleet.virtual_uavs:
        uav.step(dt)

    # 2. UGV follower advance
    if self._ugv_follower is not None:
        ctrl = self._ugv_follower.run_step()
        ugv.apply_control(ctrl)

    # 3. Check every in-flight command for completion / failure
    for cmd_id, in_flt in list(self._in_flight.items()):
        result = self._check_completion(in_flt, sim_time)
        # result ∈ {None, "completed", ("failed", reason), ...}
        if result is None:
            continue
        self._finalize_command(cmd_id, in_flt, result, sim_time)
```

`_check_completion()` 按 `kind` 分支：
- `UAV_GOTO/RTL`：`distance(uav.pose, target) ≤ UAV_ARRIVAL_EPS`
- `UGV_GOTO/RTL`：`self._ugv_follower is None or self._ugv_follower.done()`
- `UAV_PATROL` (loop=false)：`virtual_uav.path_index >= len(path)`
- `UAV_PATROL` (loop=true)：永远 None（ongoing 已在 accept 时发过）
- `UGV_EXTINGUISH`：accept 时把它放入 `awaiting="extinguish"`，下一 tick 进入这里 → destroy fire actor → completed
- `UAV_HOLD` / `UGV_STOP`：accept 时立即标记 `awaiting="instant"`，本 tick 末就完成

`_finalize_command(cmd_id, in_flt, result, sim_time)`：
- emit command_status（completed/failed/cancelled）
- emit event_log（带 cmd_id）
- `_in_flight.pop(cmd_id)` / `_in_flight_by_entity.pop(in_flt.target)`

### 6.3 各入口对内部状态的修改

| 入口 | 修改 |
|---|---|
| `setup()` | spawn UGV、注册 UAVs、绑相机；填充 `fleet.origins`；`_in_flight={}`；`_resetting=False` |
| `teardown()` | destroy 所有 actors / virtual UAVs / fire actors；清 incidents；清 in-flight；保留 FrameQueue 实例 |
| `HTTP /scenario/fire` | 检查 id 不重复 → spawn fire actor → `fleet.incidents[id]=...`、`_fire_actors[id]=actor` |
| `HTTP /scenario/reset` | 见 §5.2 |
| `cmd UAV_PATROL/GOTO/RTL/HOLD` | supersede 旧 cmd → 改 fleet.virtual_uav.path/target → in_flight 新建 |
| `cmd UGV_GOTO/RTL` | supersede 旧 cmd → 新建 follower → in_flight 新建 |
| `cmd UGV_STOP` | supersede 旧 cmd → `_ugv_follower=None` + apply brake → 标记本 cmd instant |
| `cmd UGV_EXTINGUISH` | accept 时距离 check；ok → 标记 awaiting=extinguish |

### 6.4 supersede 流程

```python
def _accept_command(self, cmd):
    # 如该 entity 有旧 in_flight，cancel 它
    old_cmd_id = self._in_flight_by_entity.get(cmd.target)
    if old_cmd_id is not None:
        old = self._in_flight[old_cmd_id]
        self._finalize_command(
            old_cmd_id, old,
            ("cancelled", "superseded", {"by_cmd_id": cmd.id}),
            sim_time
        )
    # 把新命令登记
    self._in_flight[cmd.id] = InFlightCommand(...)
    self._in_flight_by_entity[cmd.target] = cmd.id
```

**注意**：UGV_STOP 是特殊的——它 supersede 旧命令后，自己也是 instant，会在同 tick 完成。所以连发会有两条 command_status:
1. cmd_old: cancelled(reason=explicit_stop)
2. cmd_stop: completed

---

## 7. Bridge 模块改造细则

### 7.1 `carlabridge/commands/`

**`enum.py`**：8 条 CommandKind + 结构化 `RejectCommand`：
```python
class CommandKind(str, Enum):
    UAV_PATROL = "UAV_PATROL"
    UAV_GOTO = "UAV_GOTO"
    UAV_RTL = "UAV_RTL"
    UAV_HOLD = "UAV_HOLD"
    UGV_GOTO = "UGV_GOTO"
    UGV_RTL = "UGV_RTL"
    UGV_EXTINGUISH = "UGV_EXTINGUISH"
    UGV_STOP = "UGV_STOP"

class RejectCommand(Exception):
    def __init__(self, reason: str, detail: dict | None = None):
        self.reason = reason
        self.detail = detail or {}
```

`ParsedCommand`：`text → kind`、`payload → params`。

**`dispatcher.py`**：每条命令独立校验函数；reject 直接抛 RejectCommand。

**`bus.py`** 改造：
- `submit(cmd)` 不再异步 emit ack/reject —— ack/reject 现在是 sio.call 返回值
- 但 sim 域执行完后**仍然**通过 `call_soon_threadsafe` 触发 `command_status` 广播

### 7.2 `carlabridge/scenarios/s1_fire.py`

按 §6 重写。

**保留**：`setup()` 中 spawn UGV、注册 UAV、`_spawn_first_available` 工具、`teardown()`

**改写**：
- `setup()` 末尾自动 set_target UAV-01 的代码删除（现在由 Agent 下发 PATROL 触发）
- 新增 `_in_flight`、`_in_flight_by_entity`、`_run_id`、`_resetting`
- `on_command()` 重写为 8 条 dispatch + supersede 逻辑
- `on_tick_post()` 按 §6.2 重写
- 新增 `ignite_fire(...)` / `reset()` 公共方法（被 HTTP 调用）

**删除**：
- `_SCRIPT` / `ScriptEvent` / `mock_agent_loop` / `_fire_script_event` / `_materialize_payload`
- `_script_start_sim` / `_sim_time_provider` / `attach_sim_time_provider`
- `_uav_rtl/_uav_hold/_ugv_dispatch/_ugv_rtl/_resolve_ugv_destination`（旧实现，新实现合并入 dispatch）

### 7.3 `carlabridge/core/`

**`fleet.py`**：新增 `origins: dict[str, Pose]`、`incidents: dict[str, Incident]`；相应方法。

**新增 `incident.py`**：
```python
@dataclass(slots=True)
class Incident:
    id: str
    kind: str
    position: Pose
    severity: str
    since_sim_time: float
    # 无 status 字段——存在即活跃
```

**`snapshot.py`** `WorldSnapshot`：新增 `run_id`、`bridge_session_id`、`incidents`、`in_flight_commands`。

### 7.4 `carlabridge/bus/`

**`projector.py:for_agent(snap)`**：新增 §4.1 全部新字段序列化。

**`agent_ns.py`**：
- `on_agent_command(sid, payload) -> dict` 改 return-value 形式
- 新增 helpers：`broadcast_command_status(payload)`、`broadcast_scenario_event(payload)`
- 删除 `agent_ack` / `agent_reject` 旧 emit 路径
- `on_hello` 返回 `bridge_session_id`

**新增 `routes_scenario.py`**：§5 三个 HTTP 路由 + `run_in_sim_domain(fn)` 同步原语

### 7.5 `carlabridge/scenarios/runner.py`

新增 `run_in_sim_domain(fn) -> asyncio.Future`：把 callable 投到 sim 队列，完成后 `call_soon_threadsafe(future.set_result, ...)`。

### 7.6 `carlabridge/agent/` —— **整目录删除**

删除文件：
- `link.py`
- `mock_agent.py`
- `socketio_agent.py`
- `__init__.py`

不再需要任何 Bridge 内的 Agent 抽象层 —— Bridge 只有 Socket.IO handler 一条入口，不需要 AgentLink。

### 7.7 `carlabridge/config.py` + `config/default.toml`

```toml
# 删除 [agent] 段（mode 配置已无意义）

[scenario.s1_fire]
extinguish_radius_m = 5.0
default_uav_rtl_speed = 8.0
default_ugv_target_speed_kmh = 25.0
uav_arrival_eps_m = 0.5
```

### 7.8 `carlabridge/main.py`

- 删 mock_agent task 启动分支
- 挂载 `routes_scenario` 到 aiohttp app
- 启动后 Bridge 空载：UAVs 停在 origin，UGV 停在 origin，无 incident，等 Agent 接入

### 7.9 `carlabridge/commands/bus.py`

- `submit()` 保留（仍然投 sim 队列）
- 删除 `reject()` 方法（reject 现在是 sio.call return）
- 在 sim 域命令处理完毕的 callback 改为：发 `command_status` 而非 `agent_ack/reject`

---

## 8. UrbanAgent 协议参考（不在本期实施范围）

**本节仅作协议参考**。UrbanAgent 仓的具体改动由对方仓自行规划与实施。

UrbanAgent 接入 Bridge 需要遵守的协议要点：

1. **连接**：连到 `/agent` namespace，发 `hello`，从响应取 `bridge_session_id`
2. **命令下发**：`await sio.call('agent.command', envelope, namespace='/agent', timeout=…)` 拿 accepted/rejected
3. **命令完成观察**：订阅 `command_status` 事件，按 `cmd_id` 匹配
4. **状态观察**：订阅 `state.snapshot`（10 Hz）；watch `incidents`、`in_flight_commands`
5. **reset 处理**：订阅 `scenario_event`；收到 `{event:"reset"}` 立即清空内部状态、等下一帧 snapshot 看新 origins、重发巡逻
6. **会话感知**：watch snapshot 中 `run_id` 与 `bridge_session_id`：
   - run_id 变化 = reset
   - bridge_session_id 变化 = Bridge 重启（更激进的清状态）
7. **故障兜底**：snapshot.in_flight_commands 用于"我发过没收到完成事件"对账

UrbanAgent 仓 `urbanagent/carla_bridge.py` 改造方向（仅作记录、不在此 PR 范围）：
- `send_action` 改用 sio.call
- 删除 `_pending` futures
- 增加 `command_status` / `scenario_event` handler
- 翻译表更新到 8 条新命令

### 8.1 测试用 Hardcoded Agent（本仓 root，**在本期实施范围内**）

**目的**：脱离 UrbanAgent 仓也能跑通 Bridge 端到端流程；CI / 烟测 / 协议回归覆盖工具。

**与 mock_agent.py 的区别**：
- `mock_agent.py`（已删）是**嵌在 Bridge 进程内**的剧本驱动协程
- 本测试 agent 是**完全外部**的独立 Python 脚本，通过 Socket.IO 客户端连入 Bridge，与真实 UrbanAgent 走**完全相同**的协议路径
- 不会被 Bridge 启动；运行时由开发者手动 `python test_agent.py` 启动

**放置位置**：`D:\Urban_v2\CarlaBridge\test_agent.py`（仓 root，与 `run.ps1` 同级；**不**放在 `carlabridge/agent/`，那个目录已删）

**依赖**：`python-socketio[asyncio_client]`（已在 pyproject 依赖中）

**硬编码触发逻辑**：

```
连接阶段：
  - sio.connect 到 :5000 /agent
  - 发 hello {agent_id:"test-agent"} → 记录 bridge_session_id

待命阶段：
  - 等第一帧 state.snapshot
  - 从 snapshot 读 origins（或硬编码每架 UAV 的 3 个 waypoint）
  - 给 UAV-01/02/03 各发一条 UAV_PATROL（loop=true）
  - 记录返回的 cmd_id 集合（应收到 3 个 ongoing 事件）

火情响应（事件驱动）：
  - 每帧 snapshot 到达：
    - 若 incidents 中出现新 id 且本 agent 未处理：
      - 选 UGV-01，发 UGV_GOTO(dest=incident.position + offset)
      - 标记本地 _responding[incident_id] = "going"
    - 若 _responding[incident_id]="going" 且 UGV 到 incident 距离 ≤ EXTINGUISH_RADIUS：
      - 发 UGV_EXTINGUISH(incident_id)
      - _responding[incident_id] = "extinguishing"
  - 收到 command_status:completed 且 cmd_id 是某 UGV_EXTINGUISH：
    - _responding 中清除该 incident_id
    - 发 UGV_RTL

reset 响应：
  - 收到 scenario_event{event:"reset"}：
    - 清 _responding、清 cmd 跟踪
    - 等下一帧 snapshot → 重发 PATROL（同连接阶段）

日志：
  - 每个收到/发出的事件打印一行（含 cmd_id / event 名 / 状态）
  - 适合直接 stdout 看流程
```

**命令行接口**：
```bash
python test_agent.py --url http://127.0.0.1:5000 --namespace /agent
# 可选：--verbose / --no-extinguish (只发 UGV_GOTO 不灭火，用于测试 cancel 链)
```

**端到端冒烟流程**（README 文档化）：
```
# 终端 1
.\run.ps1                                # 起 Bridge
# 终端 2
python test_agent.py                     # 起测试 agent，看到 PATROL 已下发
# 终端 3
curl -X POST :5000/scenario/fire \
  -H "Content-Type: application/json" \
  -d '{"id":"fire-001","position":{"x":90,"y":0,"z":0}}'
# 观察 终端 2 输出：识别 incident → UGV_GOTO → 到达 → UGV_EXTINGUISH → completed → UGV_RTL
curl -X POST :5000/scenario/reset
# 观察 终端 2 输出：scenario_event(reset) → 重发 PATROL
```

**约束**：
- ≤ 250 LOC，单文件，零内部 import（仅 `socketio`、`asyncio`、stdlib）
- 不做任何决策栈伪装，不做 LLM 调用，不接入 UrbanAgent 包
- 所有可调参数（PATROL paths、UGV offset、轮询间隔）在文件顶部以常量声明，便于改

---

## 9. 数据流（重构后）

### 9.1 启动 → 待命

```
1. Bridge: spawn UGV / 注册 UAV / 绑相机 / fleet.origins 写入 / bridge_session_id = uuid
2. Bridge 空载等待
3. UrbanAgent 进程启动 → 连 /agent → 发 hello → 收到 session_id
4. 第一帧 snapshot 到达 → Agent 看到 incidents=[]、in_flight_commands=[]
5. Agent 决策：下发 3 条 UAV_PATROL → sio.call → accepted
6. Bridge 接到 PATROL → 设置 fleet.uav.path → 每 tick lerp 推进
7. 静态稳态：UAVs 巡逻中，UGV 停在 origin
```

### 9.2 Operator 触发火情

```
operator: curl -X POST :5000/scenario/fire -d '{"id":"fire-001","position":{"x":90,"y":0,"z":0}}'
Bridge HTTP → run_in_sim_domain(scenario.ignite_fire, ...)
            → sim 域：spawn firetruck + fleet.incidents 加 fire-001
            → 200 OK
下一帧 snapshot: incidents = [fire-001]
Agent 看到新 incident → 决策处理
```

### 9.3 Agent 处理火情

```
Agent: sio.call('agent.command', UGV_GOTO dest=fire-001.position + offset)
         → Bridge accept → in_flight[cmd-a02] 登记
         → 返回 {status:accepted}
       (期间 Agent watch snapshot 中 UGV-01 与 fire-001 距离)
       sio.call('agent.command', UGV_EXTINGUISH incident_id=fire-001)
         → Bridge: 旧 UGV_GOTO supersede → command_status:cancelled
         → 距离 check ok → in_flight[cmd-b03] 登记, awaiting=extinguish
         → 返回 {status:accepted}
       下一 tick: Bridge destroy firetruck + 移除 incident + emit command_status:completed
       下一帧 snapshot: incidents=[], in_flight_commands=[]
       Agent: 看到 command_status:completed cmd-b03 → 状态机推进
            → 发 UGV_RTL → ...
```

### 9.4 Operator 触发 reset

```
operator: curl -X POST :5000/scenario/reset
Bridge: _resetting=True
        所有 in_flight → emit command_status:cancelled(reason="reset")
        run_in_sim_domain(scenario.teardown_then_setup)
          → sim 域：destroy 所有 actors → spawn 全部 actors → 重绑相机 → fleet.origins 重写
        _run_id += 1
        _resetting=False
        emit scenario_event{reset, run_id}
        200 OK
下一帧 snapshot: run_id 变了, incidents=[], in_flight_commands=[]
Agent: 收到 scenario_event → 清内部状态 → 重发 PATROL
```

---

## 10. 风险与权衡

| 风险 | 缓解 |
|---|---|
| python-socketio 不同版本对"handler return = ack" 行为有差异 | 锁定 `python-socketio >= 5.x`；起 `tests/test_agent_command_rpc.py` 端到端覆盖 |
| 命令完成检查每 tick 跑——in_flight 多时性能 | 命令上限不超过 fleet size（每 entity 一条），常态 ≤ 10 条；线性扫描足够 |
| reset 期间 socket 帧顺序：cancelled 事件 vs scenario_event vs snapshot 谁先到 | 文档约定：Agent 必须容忍乱序；按 cmd_id 与 run_id 匹配，不依赖 wall-clock 顺序 |
| reset 后相机重绑期间视频可能黑屏几帧 | FrameQueue 保留 + 重绑通常 < 100ms；可接受（design.md §7 已注明） |
| sio.call timeout（默认 10s）小于 reset 时长 | Bridge reset 期间应在 <1s 内完成 teardown+setup；reset 不应阻塞 sio.call handler；若发生超时 Agent 会重连/重发 |
| Agent 误把"被 STOP cancel"当成"failure" | command_status.reason 严格区分；文档明确 cancelled.reason ∈ {superseded, reset, explicit_stop, bridge_shutdown} |
| Bridge 进程崩溃后 Agent 不知道 | bridge_session_id 在 snapshot 中持续推送；重连后变化的话 Agent 清空所有 in_flight 记忆 |
| HTTP /scenario/reset 与并发 sio.call 命令的竞争 | _resetting 标志为 atomic；reset 期间 sio.call 一律 reject `scenario_resetting` |

---

## 11. 删除清单（CarlaBridge 仓内）

**整目录删除**：
- `carlabridge/agent/`（所有文件）

**文件内大段删除**：
- `carlabridge/scenarios/s1_fire.py`：剧本字段、mock_agent_loop、旧命令处理函数（见 §7.2）
- `carlabridge/commands/enum.py`：删 `MARK_EVENT`、`ATTACH_ACTOR`（原 RTL/HOLD/DISPATCH 由新枚举替代，名字部分相同但语义重定义）
- `carlabridge/bus/agent_ns.py`：`agent_ack`/`agent_reject` emit 路径
- `carlabridge/commands/bus.py`：`reject()` 方法
- `config/default.toml`：`[agent] mode` 配置项

**测试清理**：
- `tests/test_agent_link.py` —— 整文件删（AgentLink 删了）
- `tests/test_s1_command.py` —— 重写
- `tests/test_scenarios.py` —— 删剧本相关 case
- `tests/test_commands.py` —— 按新 8 条命令重写

---

## 12. 测试影响 & 新增覆盖

| 范畴 | 改动 | 新测试 |
|---|---|---|
| 命令解析 | 重写 | `test_dispatcher_v2.py`：8 条命令的合法/非法 payload + RejectCommand reason 枚举 |
| 命令生命周期 | 新增 | `test_command_lifecycle.py`：accepted → completed / cancelled(superseded) / cancelled(reset) / failed |
| Instant 命令 | 新增 | `test_instant_commands.py`：HOLD/STOP/EXTINGUISH 在同 tick 完成 |
| PATROL loop=true | 新增 | `test_patrol_loop.py`：ongoing 事件 + supersede 时 cancelled |
| reset = teardown + setup | 新增 | `test_reset_reinit.py`：reset 后 actor_id 变化；entity_id 稳定；FrameQueue 实例保留；in_flight 全 cancelled |
| HTTP 端点 | 新增 | `test_routes_scenario.py`：三端点；fire 409 冲突；reset 503 并发 |
| sio.call RPC | 新增 | `test_agent_command_rpc.py`：accepted/rejected 返回值 |
| snapshot 投影 | 加 case | `tests/test_projector.py`：run_id, bridge_session_id, incidents, in_flight_commands |
| supersede 流程 | 新增 | `test_supersede.py`：同 entity 连发命令的 cancel 链 |
| 无状态机 | 新增 | `test_no_timed_events.py`：跑 100s sim_time 无任何自动事件 |

---

## 13. 实施顺序（CarlaBridge 仓 PR 拆分提案）

| 步 | 范围 | 验收 |
|---|---|---|
| R1 | 新命令 enum（8 条）+ dispatcher + 结构化 RejectCommand | dispatcher 单测 |
| R2 | `Incident` 数据模型 + `Fleet.incidents`/`Fleet.origins` + `WorldSnapshot` 新字段 + projector | snapshot 单测 |
| R3 | `InFlightCommand` 数据模型 + `_in_flight` / `_in_flight_by_entity` + supersede 流程 + `_check_completion` 框架 | 生命周期单测（无 CARLA） |
| R4 | `S1FireScenario` 重写：删剧本、加 8 命令 dispatch、`ignite_fire()` / `reset()` 公共方法 | scenario 单测 + CARLA 联跑空载 |
| R5 | `agent_ns.on_agent_command` 改 return-value（sio.call）+ `broadcast_command_status` + `broadcast_scenario_event` + 删 agent_ack/reject | socketio AsyncClient 端到端打 1 条命令 |
| R6 | aiohttp `routes_scenario.py`（fire/reset/status）+ `run_in_sim_domain` 工具 + reset 期间命令 reject 逻辑 | curl 跑通三端点；reset 时 in_flight 全 cancelled |
| R7 | 删 `carlabridge/agent/` 整目录 + 删 `[agent] mode` 配置 + main.py 简化启动 | Bridge 起服务空载等 Agent；无 import 残留 |
| R8 | bridge_session_id + UAV_PATROL ongoing 状态 + supersede 链 + reset cancel 链覆盖测试 | 全部测试通过 |
| R9 | **`test_agent.py` 测试 agent**（仓 root，§8.1）：sio 客户端 + hardcoded 触发逻辑 | 端到端冒烟流程跑通 |
| R10 | 文档同步：`design.md` §5.3/§8/§9/§10 标注 superseded；`spec.md` 加 D10 决议；README 加测试 agent 用法 | 文档评审 |

**UrbanAgent 仓改造（不在本 PR 序列内）**：由对方仓基于 §8 协议参考另起 PR。

---

## 14. 与 spec.md / design.md 的关系

| spec/design 条目 | 新决议 |
|---|---|
| `design.md` §5.3 控制流 | emit + ack/reject → sio.call return-value + command_status 事件 |
| `design.md` §8 场景引擎 | mock_agent_loop 全删；剧本概念取消；§6 状态描述替代 |
| `design.md` §9 Mock vs 真实 Agent | mock 模式整体废弃；`carlabridge/agent/` 删 |
| `design.md` §10 协议与端口 | 新增 /scenario/fire、/scenario/reset、/scenario/status |
| `spec.md` D2（mock 写死剧本） | 完全废弃 |
| `spec.md` D3（不实现机械臂） | 保留，EXTINGUISH 只做距离判定 + actor destroy |
| `spec.md` D9（BasicAgent timeout） | 保留，UGV_GOTO/RTL 走 SimpleWaypointFollower |
| `spec.md` 新增 D10 | "Bridge 时间无关 / 8 条命令 / 通用生命周期 / mock 废弃 / UrbanAgent 解耦 / HTTP 触发" |

---

## 15. 评审清单（请逐条勾认）

- [√] §1 七条目标完整反映你的意图
- [√] §2 角色分工："Bridge 完全时间无关、只被动响应"理解一致
- [√] §3.2 8 条命令的 params + completed 定义没有遗漏
- [√] §3.3 reject reason 枚举可被 Agent 程序化处理
- [√] §3.4 命令生命周期四态（completed/failed/cancelled/ongoing）以及"PATROL loop=true 走 ongoing"
- [√] §3.5 command_status × event_log 双通道分工：command_status 机器读、event_log 人读（带 cmd_id）
- [√] §4.1 snapshot 新字段：run_id、bridge_session_id、in_flight_commands；incidents 无 status 字段
- [√] §4.3 scenario_event 只承担 reset
- [√] §5 HTTP 三端点的请求/响应 schema；fire 重复 id 返回 409；reset 期间命令 reject scenario_resetting
- [√] §6 Bridge 内部"无状态机、无定时器、每 entity 一条 in-flight"模型
- [√] §7.6 `carlabridge/agent/` 整目录删除
- [√] §8 UrbanAgent 改造仅作参考，不在本仓本期范围
- [无] §10 风险表有遗漏需要补充的吗
- [不需要] §13 实施顺序合理；R7 删目录是否要更早

确认通过后，§13 R1~R9 拆进 `tasks.md`。
