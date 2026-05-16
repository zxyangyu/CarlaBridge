# Bridge × Agent 接入协议 v1.0

> 文档目的:**CarlaBridge ↔ UrbanAgent** 的唯一接口契约。
> 任何一方实现以本文档为准;两边的内部设计文档(`CarlaBridge/design*.md`、UrbanAgent 内部架构)不能覆盖本文档。
> 上游决议:`CarlaBridge/spec.md` D10(Bridge 时间无关 + 8 命令)+ 2026-05-15 评审纪要(批确认 = completed、best-effort)。

| 字段 | 值 |
|---|---|
| 协议版本 | 1.0 |
| 状态 | 待评审落地 |
| 实现方 | CarlaBridge(服务端)+ UrbanAgent(客户端) |
| 传输 | Socket.IO v4(WebSocket transport)+ HTTP/JSON(operator 控制面) |
| 端口 | 默认 `:5000`(可配) |
| Namespace | `/agent`(Agent ↔ Bridge);`/`(Frontend,**只读**,不在本文档范围) |

---

## 1. 范围

### 1.1 本协议定义
- **Agent ⇄ Bridge** 的所有 wire-level 行为:连接、握手、状态推送、命令下发、命令生命周期、错误码、扩展规约。
- Agent 端**批决策状态机的最小推荐形态**(§9),作为实现指引。
- **Operator → Bridge** 的 HTTP 控制面(§8),用于 operator 触发火情 / reset(Agent 不能调用)。

### 1.2 本协议不定义
- Agent 内部决策算法 / LLM 调度 / 多智能体协作机制。
- Bridge 内部 sim/async 双域线程模型(见 `CarlaBridge/design.md`)。
- Frontend(`/` namespace)协议——Frontend 是只读消费者,与 Agent 解耦。
- CARLA 自身配置 / 地图 / actor 模型。
- 任何实施任务、PR 拆分、里程碑——本文档是契约,实施排期在各自仓内单独维护。

### 1.3 角色边界(硬约束)

```
┌──── UrbanAgent ────┐      ┌──── CarlaBridge ────┐      ┌──── Operator ────┐
│  决策层 (WHERE/    │      │  执行层 (HOW)        │      │  剧情触发        │
│   WHEN/WHY)        │      │                      │      │                  │
│                    │ ──►  │ • spawn entity       │ ◄─── │ POST /scenario/  │
│ • 选目标 / 巡逻    │ cmd  │ • UAV lerp           │ HTTP │  fire / reset    │
│ • 派 UGV / RTL     │      │ • UGV follower       │      │                  │
│ • 编排命令批次     │ ◄──  │ • per-tick lifecycle │      └──────────────────┘
└────────────────────┘ snap └──────────────────────┘
```

**Bridge 不主动产生剧情**:无 sim_time 触发、无定时器、无内嵌决策状态机。剧情入口是 ① operator HTTP、② Agent 命令。

**Agent 不能改变沙盘配置**:不能 spawn/destroy actor,不能触发 reset,不能调换地图。

---

## 2. 连接与握手

### 2.1 连接
- Agent 作为 Socket.IO **客户端**,连入 `http://<bridge_host>:<port>` 的 `/agent` namespace。
- TLS、鉴权:本期**不做**(沿用 `CarlaBridge/design.md` 非目标)。
- 单 Agent 模型:`/agent` namespace 允许多个客户端连入,但语义上始终是"单一决策源";Bridge 把所有 inbound 命令排成一个全局队列,先到先服务。多客户端使用方需自行避免命令冲突。

### 2.2 握手 RPC

Agent 在 `connect` handler 中**必须**调用一次:

```python
ack = await sio.call("hello", {"agent_id": "<agent-id>", "version": "1.0"}, namespace="/agent", timeout=2.0)
```

Bridge 返回:

```json
{
  "server": "carlabridge",
  "version": "1.0",
  "bridge_session_id": "br-9c1a3f...",
  "scenario": "s1_fire"
}
```

字段语义:
- `version` —— 协议版本号,字面值 `"1.0"`。**全协议共用同一字段名**:envelope、hello 入参、hello 返回三处一致,语义都是"本协议版本号"。
- `bridge_session_id` —— Bridge 进程级 uuid。Agent 重连看到该值变化 = Bridge 已重启(比 reset 更激进的状态清理)。

### 2.3 断开与重连
- `python-socketio` 默认开启自动重连。
- 重连后 Agent **必须**重新调用 `hello`。
- 重连后 Bridge 会在 `on_connect` 立刻推送一帧 `state_snapshot`(单播到该 sid)。
- Agent 重连后:对账逻辑见 §9.5。

---

## 3. 消息封装(Envelope)

### 3.1 所有应用层消息使用统一 envelope

```json
{
  "version": "1.0",
  "msg_id": "uuid-v4",
  "type": "<event-name>",
  "timestamp": 1715760000.123,
  "frame": 12345,
  "sim_time": 412.34,
  "sender": "bridge" | "agent",
  "payload": { ... }
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `version` | string | 是 | 协议版本,固定 `"1.0"` |
| `msg_id` | string | 是 | uuid v4,用于日志/链路追踪 |
| `type` | string | 是 | 事件名(与 Socket.IO event 名一致) |
| `timestamp` | number | 是 | wall time,UTC epoch seconds |
| `frame` | number\|null | 推荐 | Bridge tick 计数;Agent 出站可填最近收到的 frame,缺则 null |
| `sim_time` | number\|null | 推荐 | CARLA simulation time(秒);Bridge 出站必填;Agent 出站可填 |
| `sender` | string | 是 | `"bridge"` 或 `"agent"` |
| `payload` | object | 是 | 事件实际载荷(本文档逐事件定义) |

### 3.2 Bridge 端容忍
Bridge 入站事件 handler 对 envelope 字段**容忍缺失**:只有 `payload` 是强约束,且若客户端直接传 `payload` 内容(裸 dict)也接受——Bridge 自动判定。Agent 实现应当严格按 envelope 形式发,避免歧义。

---

## 4. 数据面(Bridge → Agent 推送)

`/agent` namespace 上 Bridge **主动 emit** 的 4 类事件:

| 事件 | 推送时机 | 用途 |
|---|---|---|
| `state_snapshot` | 10 Hz 周期(`broadcast.state_hz`,可配)+ Agent connect 时单播一次 | Agent 决策的唯一真值源 |
| `command_status` | 命令生命周期状态变化时(accepted/completed/failed/cancelled/ongoing) | Agent 推进批状态机 |
| `scenario_event` | reset 完成时;未来可扩展其他 scenario 级事件 | 整体状态机重置信号 |
| `event_log` | 人类可读日志(命令相关、环境警告) | 审计 / 前端展示 / 调试 |

### 4.1 `state_snapshot`

```json
{
  "version": "1.0",
  "type": "state_snapshot",
  "sender": "bridge",
  "sim_time": 412.34,
  "frame": 12345,
  "payload": {
    "sim_time": 412.34,
    "run_id": 5,
    "bridge_session_id": "br-9c1a3f...",
    "traffic_lights": [
      {
        "id": "TL-42",
        "pose": [12.3, -45.6, 0.0],
        "phase": "green",
        "remaining_s": 7.2
      }
    ],
    "vehicles": [
      {
        "id": "UGV-01",
        "role": "dispatchable",
        "pose": [90.0, 0.0, 0.0],
        "yaw": 92.0,
        "speed": 5.5,
        "heading": 92.0,
        "battery": null
      }
    ],
    "uavs": [
      {
        "id": "UAV-01",
        "role": "patrol",
        "pose": [10.0, 20.0, 85.0],
        "altitude": 85.0,
        "heading": 45.0,
        "battery": 98.2,
        "speed": 8.0
      }
    ],
    "incidents": [
      {
        "id": "fire-001",
        "kind": "fire",
        "position": {"x": 90.0, "y": 0.0, "z": 0.0},
        "severity": "high",
        "since_sim_time": 410.10
      }
    ],
    "in_flight_commands": [
      {
        "cmd_id": "cmd-a02",
        "kind": "UGV_GOTO",
        "target": "UGV-01",
        "accepted_at_sim_time": 411.10,
        "progress": 0.42,
        "awaiting": "ugv_arrival"
      }
    ]
  }
}
```

#### 4.1.1 顶层字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `sim_time` | number | CARLA simulation 当前秒 |
| `run_id` | int | Bridge 启动以来的 reset 计数;reset 后 +1。Agent 用以识别"场景重置" |
| `bridge_session_id` | string | Bridge 进程 uuid;改变 = Bridge 重启 |
| `traffic_lights[]` | array | 全部红绿灯当前状态(**只读**,v1.0 Agent 不可控制) |
| `vehicles[]` | array | 全部有 CARLA actor 的车辆(含 UGV 与背景车);角色在 `role` |
| `uavs[]` | array | 全部虚拟 UAV(无 CARLA actor,Bridge 维护) |
| `incidents[]` | array | 当前活跃事件;**无 status 字段**——存在即 open,从数组中消失 = 已处理 |
| `in_flight_commands[]` | array | Bridge 当前在执行(accepted 未 finalize)的命令;Agent 用于重连/丢包对账 |

#### 4.1.2 pose 表示规范(**重要**)

- **`vehicles[].pose` / `uavs[].pose` / `traffic_lights[].pose`**:`[x, y, z]` **数组**(meters, CARLA 左手系)
- **`incidents[].position` / 命令 params 内的坐标**:`{"x": .., "y": .., "z": ..}` **对象**

> 这两种形式不可互换;Agent 实现解析时需区分。位置约定保留差异是因为:`pose` 来自 CARLA Transform(隐含 yaw),`position` 是逻辑事件坐标(无朝向)。

#### 4.1.3 `vehicles[]` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 稳定 entity id(reset 后不变),如 `UGV-01`、`VEH-{carla_id}` |
| `role` | string | `"dispatchable"`(Agent 可控)或 `"civilian"`(背景) |
| `pose` | `[x,y,z]` | 位置(米) |
| `yaw` | number | 朝向(度) |
| `speed` | number | 速率标量(m/s) |
| `heading` | number | 与 yaw 相同(spec §8 对齐字段) |
| `battery` | number\|null | 电池百分比;civilian 为 null |

#### 4.1.4 `uavs[]` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 如 `UAV-01` |
| `role` | string | `"patrol"` / `"follow"` / `"standby"` |
| `pose` | `[x,y,z]` | 位置(米) |
| `altitude` | number | z 的别名,便于直接读 |
| `heading` | number | 度 |
| `battery` | number | 百分比 |
| `speed` | number | 当前 cruise speed(若有 target);否则 0 |

#### 4.1.5 `traffic_lights[]` 字段(只读)

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 如 `TL-42`(取 CARLA actor.id 加前缀) |
| `pose` | `[x,y,z]` | 路口红绿灯位置 |
| `phase` | string | `"red"` / `"yellow"` / `"green"` / `"off"` / `"unknown"` |
| `remaining_s` | number | 当前 phase 剩余秒数(尽力而为) |

> v1.0 红绿灯由 CARLA world 原生逻辑驱动,自动循环。Agent **可读不可写**。未来版本扩展 TL 控制命令时,本字段集合会加 `override` 子对象。

#### 4.1.6 `incidents[]` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | operator 指定或 Bridge 自动 `fire-<uuid8>` |
| `kind` | string | `"fire"`(v1.0 唯一支持);v1.x 可扩展 `"crowd"` `"accident"` 等 |
| `position` | `{x,y,z}` | 事件位置 |
| `severity` | string | `"low"` / `"medium"` / `"high"` / `"critical"`,自由文本可扩展 |
| `since_sim_time` | number | incident 创建时的 sim_time;Agent 用以估算事件年龄 |

> **无 `status` 字段**。incident 出现在数组中 = open;从数组中消失 = 已处理(通常因 `UGV_EXTINGUISH` 完成)。

#### 4.1.7 `in_flight_commands[]` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `cmd_id` | string | 命令 id(Agent 发命令时指定,见 §5.1) |
| `kind` | string | 命令枚举(见 §6) |
| `target` | string | entity id |
| `accepted_at_sim_time` | number | 接受时的 sim_time |
| `progress` | number\|null | 可选 0..1;仅 GOTO/RTL 类(距离比例);瞬时/ongoing 类为 null |
| `awaiting` | string | 内部状态机标签:`"instant"`/`"uav_arrival"`/`"ugv_arrival"`/`"extinguish"`/`"patrol_finish"`/`"ongoing"` |

### 4.2 `command_status`

```json
{
  "version": "1.0",
  "type": "command_status",
  "sender": "bridge",
  "sim_time": 425.10,
  "frame": 12780,
  "payload": {
    "cmd_id": "cmd-a02",
    "status": "completed",
    "kind": "UGV_GOTO",
    "target": "UGV-01",
    "reason": null,
    "detail": null,
    "at_sim_time": 425.10
  }
}
```

#### 4.2.1 `status` 枚举与触发

| status | 何时推送 | 是否终态 |
|---|---|---|
| `ongoing` | 命令 accept 后,若该命令**永不自动 completed**(`UAV_PATROL` loop=true),Bridge 立刻 emit 一次 | 否(可被 cancelled) |
| `completed` | 命令自然到达完成条件(见 §6 各命令的 *completed 定义*) | 是 |
| `failed` | 命令执行期间出错(follower 崩溃 / actor 销毁 / 内部异常) | 是 |
| `cancelled` | 被同 entity 新命令 supersede / operator reset / Bridge shutdown / 显式 STOP | 是 |

`ongoing` 是 Bridge 给 Agent 的明示信号:"这条命令不会自动 completed,你别死等"。Agent 状态机收到 ongoing 应当**视该命令为已成功推进**,继续 batch 后续逻辑(详见 §9)。

#### 4.2.2 `reason` 枚举(仅 cancelled / failed 必填)

**cancelled.reason**:

| reason | 触发 |
|---|---|
| `superseded` | 同 entity 收到新命令,旧命令立即 cancel |
| `reset` | operator 触发 `POST /scenario/reset`,所有 in-flight 一次性 cancel |
| `explicit_stop` | UGV_STOP / UAV_HOLD 接管该 entity(同 supersede 但语义清晰) |
| `bridge_shutdown` | Bridge 优雅退出,清空 in-flight |

**failed.reason**:

| reason | 触发 |
|---|---|
| `follower_error` | UGV waypoint follower 抛异常 |
| `entity_destroyed` | 命令执行期间 entity 被销毁(罕见,通常发生在 reset 边界) |
| `internal_error` | 其他未分类异常(`detail.message` 带原始信息) |

#### 4.2.3 顺序与原子性
- 同一 cmd_id 的状态序列严格单调:**最多** `ongoing` → 终态 之一,或直接发终态。
- 不同 cmd_id 的 status 顺序**不保证**(socket 帧顺序不保证);Agent 必须按 cmd_id 匹配,不能假设时序。
- `command_status` 与 `state_snapshot` 之间也不保证顺序;典型情况是 status 先于下一帧 snapshot 到达,但不强约束。

### 4.3 `scenario_event`

```json
{
  "version": "1.0",
  "type": "scenario_event",
  "sender": "bridge",
  "sim_time": 423.50,
  "payload": {
    "event": "reset",
    "run_id": 6,
    "trigger": "http"
  }
}
```

v1.0 仅承担一种事件:`reset`。Bridge 在 operator 触发 `POST /scenario/reset` 完成后广播。Agent 收到后**必须**:
1. 清空内部决策状态(batch 状态机回 IDLE)。
2. 等下一帧 `state_snapshot`(其 `run_id` 与本事件一致),从中读取新的 entity origins。
3. 重新进入决策循环。

v1.x 保留扩展:`event` 可加 `paused` / `resumed` / `scenario_changed`,Agent 实现应对未知 event 名**容忍**(不崩溃,记日志即可)。

### 4.4 `event_log`

```json
{
  "version": "1.0",
  "type": "event_log",
  "sender": "bridge",
  "sim_time": 420.15,
  "payload": {
    "severity": "ok",
    "source": "BRIDGE",
    "message": "UGV-01 arrived at (90, 0, 0)",
    "cmd_id": "cmd-a02"
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `severity` | string | `"info"` / `"ok"` / `"warn"` / `"danger"` |
| `source` | string | `"BRIDGE"` / `"SCENARIO"` / `"CARLA"` / `"AGENT"`(Agent 自己 emit 的回流) |
| `message` | string | 自由文本 |
| `cmd_id` | string\|null | 与命令相关时填;环境警告类为 null |

**Agent 决策不应当依赖 event_log**(命令决策走 `command_status`,状态决策走 `state_snapshot`)。`event_log` 是审计与人机界面通道。

---

## 5. 控制面(Agent → Bridge)

### 5.1 `agent.command`(RPC)

Agent 发送命令使用 **sio.call**(同步 RPC,等待 Bridge 返回值):

```python
ack = await sio.call(
    "agent.command",
    envelope,  # 见 §3.1
    namespace="/agent",
    timeout=2.0,
)
```

> **注意**:wire-level 事件名是带点的 `"agent.command"`;Bridge 内部 handler 名是 `on_agent_command`,handler 通过事件别名映射。Agent 端**必须**用字面值 `"agent.command"` 调用 `sio.call`。

#### 5.1.1 `payload` 结构

```json
{
  "id": "cmd-9f1",
  "kind": "UAV_GOTO",
  "target": "UAV-01",
  "priority": "normal",
  "params": {
    "waypoint": {"x": 10.0, "y": 20.0, "z": 85.0},
    "cruise_speed": 8.0
  }
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | Agent 分配的命令 id,要求全局唯一(uuid 或 `agent-prefix-<seq>`);Bridge 用此 id 作为后续 `command_status` 关联键 |
| `kind` | string | 是 | 命令枚举(见 §6) |
| `target` | string | 是 | entity id(`UAV-*` / `UGV-*`) |
| `priority` | string | 否 | `"normal"` / `"high"` / `"urgent"`;v1.0 Bridge **不据此排序**(预留扩展) |
| `params` | object | 是 | 命令特定参数(见 §6 每条命令) |

#### 5.1.2 返回值(同步 RPC ack)

**accepted**:

```json
{
  "status": "accepted",
  "cmd_id": "cmd-9f1",
  "queued_at_sim_time": 412.34
}
```

**rejected**:

```json
{
  "status": "rejected",
  "cmd_id": "cmd-9f1",
  "reason": "not_in_range",
  "detail": {"distance_m": 18.7, "max_m": 5.0}
}
```

`reason` 枚举见 §10。`detail` 是可选 dict,reason 特定字段。

#### 5.1.3 RPC 超时与重试
- 推荐 `timeout=2.0` 秒;Bridge 正常处理远低于 100ms。
- 超时**不代表命令未被处理**——只代表 ack 未及时返回。Agent 应当:
  1. 短期内不要重发同一 cmd_id(可能产生重复)。
  2. 检查下一帧 `state_snapshot.in_flight_commands` 是否包含该 cmd_id;
     - 有 → 命令已被 accept,继续等 `command_status`。
     - 无 → 命令被丢失,可重试(用同一 cmd_id 或新 cmd_id 均可——Bridge 是幂等读但不去重)。

### 5.2 `event_log`(可选,Agent → Bridge)

Agent 可主动 emit `event_log`(单向,无 ack)用于把自己的决策写入 Bridge 的 event 缓冲:

```python
await sio.emit("event_log", envelope, namespace="/agent")
```

Bridge 把 `payload.source` 覆盖为 `"AGENT"`,转发到 `event_log` 环形缓冲,前端可见。

---

## 6. 命令清单 v1.0

8 条命令,分 2 类。**所有命令的"完成定义"统一通过 `command_status` 事件传达**(§4.2)。

### 6.1 UAV 类(4 条;target = UAV id)

| `kind` | `params` | completed 触发 | Notes |
|---|---|---|---|
| `UAV_PATROL` | `path: [{x,y,z}, ...]` (≥1);`cruise_speed: number`(m/s);`loop: bool=false` | `loop=false`:走完最后 waypoint;`loop=true`:**永不自动 completed**(accept 后立即 emit `ongoing`) | 同 entity 再下发 PATROL → supersede |
| `UAV_GOTO` | `waypoint: {x,y,z}`;`cruise_speed: number` | UAV 距 waypoint ≤ `UAV_ARRIVAL_EPS`(默认 0.5m,可配) | 到达后悬停 |
| `UAV_RTL` | `cruise_speed?: number`(可选,缺省读配置 `default_uav_rtl_speed`) | UAV 距其 origin ≤ `UAV_ARRIVAL_EPS` | Bridge 从 `fleet.origins[uav_id]` 取目标 |
| `UAV_HOLD` | (无) | 同 tick 内 `accepted` + `completed`(instant) | 清 target + path,等价 supersede 当前 |

### 6.2 UGV 类(4 条;target = UGV id)

| `kind` | `params` | completed 触发 | Notes |
|---|---|---|---|
| `UGV_GOTO` | `dest: {x,y,z}`;`target_speed?: number`(km/h,缺省读配置) | `SimpleWaypointFollower.done() == True` | 内部用 `GlobalRoutePlanner` 建路径 |
| `UGV_RTL` | `target_speed?: number` | follower 到达 `fleet.origins[ugv_id]` | |
| `UGV_EXTINGUISH` | `incident_id: string` | accept 后下一 tick:fire actor destroyed + 从 `fleet.incidents` 移除 → completed | accept 时检查 UGV 距 incident ≤ `EXTINGUISH_RADIUS_M`(默认 5m),超距 reject `not_in_range` |
| `UGV_STOP` | (无) | 同 tick `accepted` + `completed`(instant) | 清 follower + apply brake,等价 supersede |

### 6.3 跨 entity 行为规则(对所有 8 条命令一致)

- **每个 entity 同时只有 1 条 in-flight 命令**:新命令到达,自动 supersede 同 entity 旧命令(emit `command_status:cancelled(reason="superseded")`)。
- **同一批中给同一 entity 发两条 = 后一条覆盖前一条**:Agent 应避免这种用法(状态机层面,见 §9)。
- **不同 entity 的命令完全独立**:无锁,无串行。

### 6.4 命令枚举的扩展规约

未来扩展命令:
- v1.x 可新增命令(只增不删)。新命令的 `kind` 形如 `<DOMAIN>_<VERB>`,例如:`TL_SET_PHASE`、`TL_RELEASE`、`PED_SPAWN`、`UGV_TRACK`。
- Bridge 收到未知 `kind` → 返回 `rejected{reason: "parse_error", detail: {field: "kind", value: "..."}}`。
- Agent 实现应当对收到的未知 `command_status.kind` **容忍**(不崩溃,记日志即可)。

---

## 7. 命令生命周期(完整状态图)

```
                         (Agent: sio.call agent.command)
                                       │
                                       ▼
                                ┌──────────────┐
                                │  parse OK?   │
                                └──────┬───────┘
                              No │           │ Yes
                                 ▼           ▼
                  ╔══════════════════════════════════════╗
                  ║   sio.call return = "rejected"       ║◄─── 终止
                  ║   reason ∈ {parse_error,             ║
                  ║     unknown_target, kind_target_     ║
                  ║     mismatch, unknown_incident,      ║
                  ║     not_in_range, no_origin,         ║
                  ║     scenario_resetting, overloaded,  ║
                  ║     internal_error}                  ║
                  ╚══════════════════════════════════════╝
                                       │ Yes
                                       ▼
                  ╔══════════════════════════════════════╗
                  ║   sio.call return = "accepted"       ║
                  ║   { cmd_id, queued_at_sim_time }     ║
                  ╚══════════════════════════════════════╝
                                       │
                                       ▼
                          ┌────────────────────────┐
                          │ Long-running?          │
                          │  • UAV/UGV_GOTO        │
                          │  • UAV/UGV_RTL         │
                          │  • UAV_PATROL          │
                          │  • UGV_EXTINGUISH      │
                          └─────────┬──────────────┘
                                    │
                       ┌────────────┴────────────────┐
                       │                             │
                  loop=true                    GOTO/RTL/PATROL(loop=false) /
                                               EXTINGUISH
                       │                             │
                       ▼                             ▼
              ┌─────────────────┐            ┌─────────────────────┐
              │ emit command_   │            │ wait for completion │
              │ status:ongoing  │            │ condition (each tick│
              └────────┬────────┘            │ Bridge checks)      │
                       │                     └──────────┬──────────┘
                       │                                │
                       │              ┌─────────────────┼─────────────────┐
                       │              │                 │                 │
                       ▼              ▼                 ▼                 ▼
              ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
              │ (waits for   │ │ completion   │ │ supersede /  │ │ exec error   │
              │  cancel)     │ │ reached      │ │ reset /      │ │ (follower    │
              │              │ │              │ │ shutdown /   │ │  crash,      │
              │              │ │              │ │ STOP/HOLD    │ │  actor       │
              │              │ │              │ │              │ │  destroyed)  │
              │              │ ▼              │ ▼              │ ▼
              │       ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
              │       │ command_     │ │ command_     │ │ command_     │
              │       │ status:      │ │ status:      │ │ status:      │
              │       │ completed    │ │ cancelled    │ │ failed       │
              │       └──────────────┘ └──────────────┘ └──────────────┘

           ┌─────────────────────────────────────┐
           │ Instant commands: UAV_HOLD/UGV_STOP │
           │                                     │
           │  accept ──同 tick──► completed      │
           │  (supersede 同 entity 旧命令同时    │
           │   emit cancelled)                   │
           └─────────────────────────────────────┘
```

---

## 8. Operator 控制面(HTTP)

> 仅 operator(人 / curl / cron / GUI)调用;**Agent 不应当调用**这些端点。Agent 决策只通过 §5 命令面影响沙盘。

### 8.1 `POST /scenario/fire` —— 触发火情

```http
POST /scenario/fire
Content-Type: application/json

{
  "id": "fire-001",
  "position": {"x": 90.0, "y": 0.0, "z": 0.0},
  "kind": "fire",
  "severity": "high"
}
```

- `position` 必填;其余字段可选。
- 响应 200:`{status:"ok", incident_id, spawned_actor_id, spawned_at_sim_time, run_id}`
- 响应 400 / 409 / 503 见 `CarlaBridge/design-refactor-agent-boundary.md` §5.1。
- 副作用:Bridge spawn fire actor + `fleet.incidents` 加入。下一帧 `state_snapshot.incidents[]` 中可见 → Agent 自然感知。

### 8.2 `POST /scenario/reset` —— 完整重新初始化

```http
POST /scenario/reset
Content-Type: application/json

{}
```

副作用(原子序列):
1. Bridge 进入 `_resetting=True`(此期间所有 `agent.command` reject `scenario_resetting`)。
2. 所有 in-flight 命令一次性 `command_status:cancelled(reason="reset")`。
3. `scenario.teardown()` + `scenario.setup()`(destroy 全部 actors → re-spawn → 重写 `fleet.origins`)。
4. `run_id += 1`。
5. 退出 `_resetting`。
6. 广播 `scenario_event{event:"reset", run_id, trigger:"http"}`。
7. HTTP 返回 200,`{status:"ok", run_id, reset_at_sim_time, cancelled_commands, destroyed_incidents}`。

### 8.3 `GET /scenario/status` —— 状态快照(便于 operator 调试)

直读内存,**不进 sim 域**;Agent 可作为重连后对账的兜底,但首选还是订阅 `state_snapshot`。

---

## 9. Agent 批决策状态机(推荐实现)

> 本节定义 Agent 端的**推荐**状态机形态,目的是把"决策一次 = 一批命令 = 一批确认"语义落到协议事件上。其他实现形态(如纯响应式)只要兼容协议事件就行,但本节是 UrbanAgent 推荐落地形态。

### 9.1 关键设计决策(本协议已锁定)

- **批确认 gate = 全部命令到达终态或 ongoing**(决议:2026-05-15 Q1)。
  - 终态 = `completed` / `failed` / `cancelled`。
  - `ongoing` = 长期运行命令(`UAV_PATROL loop=true`),不阻塞 batch 推进,但 Agent 应继续观察其后续 `cancelled` / `failed`。
- **批失败 = best-effort**(决议:2026-05-15 Q2):Bridge 独立处理每条命令;Agent 在下一轮决策时根据 `command_status` 结果调整。
- **批 = Agent 内部概念**:**协议无 batch_id 字段**。Agent 自己用 cmd_id 集合归并。

### 9.2 状态定义

```
                  decision triggered
                  (new snapshot,
                   incident appears,
                   ongoing command's
                   condition met)
                          │
            ┌─────────┐   │
            │  IDLE   │───┘
            │         │
            └────┬────┘
                 │
                 ▼
            ┌─────────┐
            │DECIDING │  ← 跑决策算法(LLM / 规则 / 调度策略)
            │         │    输出:N 条命令(批)
            └────┬────┘
                 │ (N = 0 → 回 IDLE)
                 ▼
            ┌─────────────┐
            │ DISPATCHING │  ← 顺序或并发 sio.call agent.command
            │             │    收集 N 个 ack(accepted / rejected)
            └─────┬───────┘
                  │
        ┌─────────┴─────────┐
        │ 所有 rejected?    │
        │ (空批或全失败)    │
        └───┬────────┬──────┘
            │Yes     │No
            ▼        ▼
       ┌────────┐ ┌──────────┐
       │REVIEW  │ │ AWAITING │  ← 收到 accepted 的命令进入此态
       │(下一轮)│ │          │    等其 command_status 到终态/ongoing
       └────────┘ └────┬─────┘
                       │
              ┌────────┴──────────┐
              │ 全部命令进入终态  │
              │ 或 ongoing?       │
              └─────┬─────────────┘
                    │Yes
                    ▼
              ┌──────────┐
              │  REVIEW  │  ← 检查结果:有 failed/cancelled 吗?
              │          │    有 incident 未处理?触发下一轮决策
              └────┬─────┘
                   │
                   ▼
              (回 IDLE 等下一次触发)
```

### 9.3 状态流转规则

| 当前态 | 触发事件 | 下一态 | 副作用 |
|---|---|---|---|
| IDLE | 新 snapshot 到达 / 触发条件成立 | DECIDING | 调用决策算法 |
| DECIDING | 决策完成(产出 batch=N 条) | DISPATCHING(N>0)/ REVIEW(N=0) | 记录 batch 内 cmd_id 集合 |
| DISPATCHING | sio.call 完成 | AWAITING(若有 accepted)/ REVIEW(全 rejected) | 把 accepted 的 cmd_id 加入 watch 集 |
| AWAITING | `command_status` 到达且 cmd_id ∈ watch 集 | AWAITING(部分) / REVIEW(全部进入终态或 ongoing) | 标记该 cmd_id 的结局 |
| AWAITING | `scenario_event(reset)` | IDLE | **清空所有 watch 集**;丢弃 batch |
| AWAITING | `bridge_session_id` 变化 | IDLE | 同上,且重新 hello |
| AWAITING | wall time 超过 `BATCH_TIMEOUT` | REVIEW | 标记未到达终态的 cmd 为"超时"(本地标记,Bridge 端可能仍 in-flight) |
| REVIEW | 决定下一步 | IDLE / DECIDING | 若有未处理 incident / 未达成目标 → DECIDING;否则 IDLE |

### 9.4 ongoing 的处理(关键)

`UAV_PATROL loop=true` 是**长期运行**命令——Bridge 永远不会自动 emit `completed`。Agent 状态机:

- 把 `ongoing` 视为"该命令进入运维背景态"——**不再阻塞当前 batch**。
- 但仍需保留对该 cmd_id 的关注:它**可能后续被 `cancelled` / `failed`**(被新命令 supersede / Bridge crash / reset),Agent 应当订阅这些后续状态用于:
  - 重新派任务(supersede 是 Agent 主动行为 → 已知;若是 reset 触发的 cancel → 应重发 PATROL)。
  - 监控异常(failed 表示 Bridge 出问题)。
- 建议:Agent 维护**两张表**:
  - `_active_batch`:当前 batch 等待 confirm 的 cmd_id 集合;ongoing 后从此表移除(进入"背景")。
  - `_ongoing`:已 ongoing 的 cmd_id;后续收到 cancelled/failed 时检查此表决定补救。

### 9.5 重连与对账

Agent 重连后:
1. 重发 `hello`。
2. 等下一帧 `state_snapshot`。
3. 比较 `bridge_session_id`:
   - **变化** → Bridge 已重启:`_active_batch` 与 `_ongoing` 全部作废,回 IDLE 触发全新决策。
   - **不变** → 用 `snapshot.in_flight_commands` 对账本地状态;不在 in_flight 的 cmd_id 视为终态(在断网期间已 completed 或 cancelled,但 status 事件丢了)。
4. 比较 `run_id`:
   - **变化** → 跟看到 `scenario_event:reset` 等价处理(可能 Agent 在重连期间错过了 reset 事件)。

### 9.6 批次时序约束

- Agent 在 DISPATCHING 阶段**可以并发**发出多个 `sio.call`(python-socketio 同一 namespace 支持并发 call)——但要注意 Bridge 端是顺序处理的,真正并发好处不大;实现简单点串行发也可以。
- DISPATCHING **不允许跨 batch 交错**——前一批 AWAITING 未结束,不应当进入新的 DECIDING。例外:`scenario_event:reset` 或 `bridge_session_id` 变化时强制中断。
- 同一 batch 内**不应当给同一 entity 下两条命令**——会引起 Bridge 端 supersede 链,语义混乱。Agent 决策算法层面避免。

---

## 10. 错误码总表

### 10.1 命令拒绝 `reason`(sio.call return)

| reason | HTTP 类比 | 触发 | detail 字段 |
|---|---|---|---|
| `parse_error` | 400 | payload schema 错(字段缺失 / 类型错 / kind 未知) | `{field, value?}` |
| `unknown_target` | 404 | `target` 不在 `fleet` 中 | `{target}` |
| `kind_target_mismatch` | 400 | UAV 命令但 target 是 UGV,反之 | `{kind, target_kind}` |
| `unknown_incident` | 404 | UGV_EXTINGUISH 的 `incident_id` 不存在 | `{incident_id}` |
| `not_in_range` | 422 | UGV_EXTINGUISH 距 incident 超过阈值 | `{distance_m, max_m}` |
| `no_origin` | 500 | *_RTL 时 entity 无 origin(理论不应发生) | `{entity_id}` |
| `scenario_resetting` | 503 | reset 进行中 | (空) |
| `overloaded` | 503 | command bus 已满 | `{queue_size}` |
| `internal_error` | 500 | 其他异常 | `{message}` |

### 10.2 命令终态 reason(`command_status`)

见 §4.2.2(cancelled.* 与 failed.*)。

### 10.3 HTTP 控制面错误(operator,与 Agent 无关)

见 §8 各端点。

---

## 11. 扩展规约

### 11.1 协议版本号
- 单一字段名 `version`,在 envelope、hello 入参、hello 返回中统一使用。
- 主版本号(`major`)变更 = **破坏性变更**。Bridge 检测到 Agent 声明的主版本与自身不一致时,写一条 `event_log{severity:"warn", source:"BRIDGE"}` 提示版本错配,但**不主动断连**——是否继续由 Agent 自行决定(典型做法:Agent 自检 `hello` 返回的 `version`,主版本不匹配则主动 disconnect)。
- 次版本号(`minor`)变更 = **向后兼容**。低版本 Agent 连接高版本 Bridge 时,Bridge 多出来的新字段 Agent 不读即可,不影响功能。
- 本期 `version = "1.0"`。

### 11.2 字段扩展原则
- 数据面(snapshot / command_status / event_log / scenario_event)**只增字段,不改语义**。
- Agent 实现应当**容忍未知字段**(JSON 解析丢弃即可)。
- 控制面(`agent.command`)的 `kind` 与 `reason` 是开放枚举——只增不删。

### 11.3 v1.x 已规划扩展点

| 扩展 | 形态预告 |
|---|---|
| 红绿灯控制 | 新命令 `TL_SET_PHASE { phase: red\|yellow\|green }`(ongoing 长任务)+ `TL_RELEASE`(instant);snapshot.traffic_lights[] 增 `override: {cmd_id, locked_phase}` 子字段 |
| 行人 actor | snapshot 增 `pedestrians[]`;新命令 `PED_SPAWN`(operator)/ `PED_TRACK`(Agent 跟踪) |
| Incident 类型 | `incident.kind` 增 `"accident"` / `"crowd"` 等 |
| 协同导引 | 新命令 `UGV_FOLLOW_UAV { uav_id, offset, max_speed }` |
| Bridge 主动 ping | 新事件 `bridge_ping`(场景节奏感知) |
| 多 Agent | namespace 仍是 `/agent`,但增握手字段 `role`;命令通道按 entity 划分锁 |
| 视频通道协同 | snapshot 增 `cameras[].binding`,Agent 可发 `CAM_REBIND` |

### 11.4 不在扩展范围(明确不做)
- Agent 主动 spawn / destroy actor。
- Agent 触发 reset。
- 跨进程 batch 原子性(永远 best-effort)。
- 协议层的端到端加密 / 鉴权(部署侧解决)。

---

## 12. 一致性不变量

每条命令实现都必须满足以下不变量(任一违反 = bug):

1. **每个 `accepted` 必有一次终态或 `ongoing` 后续 emit**(除非 Bridge crash)。
2. **`ongoing` 后只可能有 `cancelled` 或 `failed`**(不会突然 `completed`)。
3. **同 entity 任意时刻 in-flight 数 ≤ 1**;违反则 supersede 流程有 bug。
4. **`scenario_event:reset` emit 之后,所有上一 run_id 的 in-flight 都已 `cancelled`**(reason=reset)。
5. **`bridge_session_id` 不变期间,`run_id` 单调不减**。
6. **`state_snapshot.in_flight_commands` 与 Bridge 内部 `_in_flight` 严格一致**(每 tick rebuild)。
7. **`UGV_EXTINGUISH` 完成后,`incident_id` 不再出现在下一帧 `snapshot.incidents`**。

---

## 13. 决议引用

| 决议 | 来源 | 影响章节 |
|---|---|---|
| Bridge 完全时间无关 | `CarlaBridge/spec.md` D10 | §6 |
| 8 命令收敛 + sio.call return-value | `CarlaBridge/spec.md` D10 | §5.1、§6 |
| 批确认 = command_status:completed | 2026-05-15 评审(Q1) | §9 |
| 批失败 = best-effort | 2026-05-15 评审(Q2) | §9.1 |
| 红绿灯控制 v1.0 不纳入 | 2026-05-16 评审 | §6(不含 TL 命令)、§11.3(列为扩展) |

---

## 14. 评审清单

- [√] §3 envelope 与 §5.1 命令 payload 字段一致
- [√] §4 数据面 4 类事件齐全 + 字段含义清晰
- [√] §6 命令清单 8 条 + completed 定义完整
- [√] §7 lifecycle 状态图覆盖所有 status × kind 组合
- [√] §9 Agent 状态机能处理:正常 batch、含 ongoing、含 reject、reset、重连
- [√] §11 扩展规约对 v1.x 路径有路标
- [√] §12 一致性不变量可作为两侧实现的 acceptance criteria
