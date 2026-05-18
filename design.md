# CarlaBridge 架构设计（design.md）

> 文档目的：定义 **当前** 的 HOW —— 模块结构、并发模型、数据流、技术栈。
> 上游契约：[`bridge-agent-protocol-v1.md`](./bridge-agent-protocol-v1.md)（线协议）。
> 入口与运维：[`README.md`](./README.md)。
> 历史决议：[`docs/archive/`](./docs/archive/)（v0.1 spec/design + refactor v0.3 + R1~R11 changelog）。

| 字段 | 值 |
|---|---|
| 版本 | v0.3 (refactor v0.3 + R11 envelope) |
| 状态 | 与代码一致（截至 2026-05-16） |
| 语言 | Python 3.12 / Windows 10 / CARLA 0.9.16 |

---

## 1. 设计总则

| 条目 | 说明 |
|---|---|
| **CARLA tick 永不阻塞** | 30 Hz 主循环；任何下游 I/O 不能反压 sim 域（NF1，目标值；硬件不足时降频但不卡） |
| **三层独立频率** | CARLA tick / 视频帧 / 状态广播 各自速率，相互解耦 |
| **Bridge 完全时间无关** | 不内嵌剧本、不监听 sim_time、不做自动事件 — 所有 state transition 由命令或 HTTP 触发 |
| **唯一控制源 = Agent** | 通过 `/agent` Socket.IO namespace；前端 `/` namespace **只读** |
| **Operator 唯一沙盘控制点** | HTTP `/scenario/{fire,reset,status}`；Agent 不能调用 |
| **每 entity 单条 in-flight 命令** | 新命令到达即 supersede 旧命令 |
| **简单优先** | 单进程 / 一条 tick 线程 + 一个 asyncio loop；不引入 Redis / ZMQ / 多进程 |

非目标：< 100 ms 端到端延迟、GPU 编码、热加载、认证、TLS、回放。

---

## 2. 高层架构

```
┌──────────────────────── CarlaBridge (Python 3.12) ────────────────────────┐
│                                                                           │
│  Sim domain (single thread)         Async domain (asyncio loop)           │
│  ┌──────────────────────────┐       ┌──────────────────────────────┐      │
│  │ TickLoop                 │ snap  │ Broadcaster (10 Hz)          │──┐   │
│  │  ├─ drain command_bus    │──ref──▶ Socket.IO server             │  │   │
│  │  ├─ scenario.on_tick_pre │       │   ├─ /        (frontend, ro) │◀─┼── React 前端
│  │  ├─ world.tick()         │       │   └─ /agent   (Agent, rw)    │◀─┼── UrbanAgent
│  │  ├─ snapshot.build()     │       │ aiohttp routes               │  │   │
│  │  ├─ scenario.on_tick_post│       │   ├─ /healthz, /debug/events │  │   │
│  │  └─ pacing               │ cmd   │   ├─ /webrtc/{cam}, /video_  │  │   │
│  │                          │◀──────┤   │   feed (MJPEG)           │  │   │
│  │ Scenario (S1FireScenario)│ queue │   ├─ /admin/shutdown         │◀─┼── Operator
│  │  ├─ _in_flight, fleet    │       │   └─ /scenario/{fire,reset,  │  │   │
│  │  ├─ origins, incidents   │       │       status}                │  │   │
│  │  └─ ignite_fire/reset    │       │ aiortc (VP8 software encode) │  │   │
│  └──────────┬───────────────┘       └──────────────────────────────┘  │   │
│             │ frame                                                   │   │
│             ▼                                                         │   │
│  CARLA sensor cb (CARLA threads) ─► FrameQueue[N] (latest-1) ─► Track │   │
│                                                                       │   │
└────────────────────────────┬──────────────────────────────────────────┘   │
                             │ Python API                                   │
                             ▼                                              │
                       ┌──────────────┐                                     │
                       │ CARLA Server │                                     │
                       └──────────────┘                                     │
```

**两个执行域**：
- **Sim domain**（单条专属线程）：拥有 CARLA 主循环，调 `world.tick()`、构建 `WorldSnapshot`、跑 `scenario.on_tick_*`、推进 `_in_flight` 命令、emit `command_status` / `scenario_event`（通过 `loop.call_soon_threadsafe`）。
- **Async domain**（main thread asyncio loop）：托管 aiohttp + python-socketio + aiortc，处理所有 I/O。

**跨域只有四条通道**：

| 数据 | 方向 | 容器 | 满 / 空策略 |
|---|---|---|---|
| `WorldSnapshot` | sim → async | 单值 atomic ref | 永远只读最新 |
| `Frame[cam_id]` | sim → async | bounded=1 latest-wins | 旧帧立即丢弃，drop_count → metrics |
| `Command` (RPC) | async → sim | `queue.Queue(maxsize=64)` | 满 → sio.call return `{rejected, reason:"overloaded"}` |
| `command_status / scenario_event / event_log` | sim → async | `loop.call_soon_threadsafe(start_background_task, ...)` | 在 async 域统一 emit 给 `/agent` |

**关键纪律**：sim 线程**绝不**直接 `sio.emit` / `await` —— 一律通过 `call_soon_threadsafe` 投递。

---

## 3. 模块分解（与 `carlabridge/` 一一对应）

```
carlabridge/
├── core/
│   ├── world.py            # CARLA 连接、sync mode 开关、ensure_map
│   ├── tick_loop.py        # 30 Hz 节拍驱动（独立线程）+ NoopScenario
│   ├── clock.py            # SimClock：sim_time / wall_time / tick_count
│   ├── atomic.py           # AtomicRef[T]，单变量赋值原子语义
│   ├── snapshot.py         # WorldSnapshot dataclass + SnapshotBuilder
│   ├── fleet.py            # CarlaActorMember + VirtualMember + origins + incidents
│   └── incident.py         # Incident dataclass（无 status，存在即活跃）
├── sensors/
│   ├── camera.py           # CameraBinding / CameraSpec / CameraManager（spawn/rebind/detach）
│   └── frame_queue.py      # 单元素 latest-wins 队列（有 drop counter）
├── streaming/
│   ├── webrtc.py           # aiortc VideoStreamTrack + signaling 路由
│   ├── mjpeg.py            # multipart/x-mixed-replace 兜底路由
│   └── jpeg_tap.py         # 按需 JPEG 编码
├── bus/
│   ├── server.py           # python-socketio AsyncServer + aiohttp app 装配 + /healthz
│   ├── envelope.py         # 协议 v1.0 envelope wrap/unwrap helper
│   ├── frontend_ns.py      # `/` namespace（前端，裸 payload）
│   ├── agent_ns.py         # `/agent` namespace（envelope + RPC handler）
│   ├── broadcaster.py      # 10 Hz 状态扇出 + 投影
│   ├── projector.py        # WorldSnapshot → 前端/Agent 投影
│   └── routes_scenario.py  # POST /scenario/{fire,reset} + GET /scenario/status
├── commands/
│   ├── enum.py             # CommandKind 8 条 + ParsedCommand + RejectCommand
│   ├── dispatcher.py       # 协议 §6 → ParsedCommand 校验
│   └── bus.py              # 跨域 queue.Queue + on_command_status / on_scenario_event 回调
├── scenarios/
│   ├── base.py             # Scenario ABC + _accept_command + _drive_command_lifecycle
│   ├── runner.py           # ScenarioRunner（生命周期 + run_in_sim_domain）
│   ├── in_flight.py        # InFlightCommand dataclass + CompletionResult
│   ├── s1_fire.py          # S1 — 8 命令 dispatch + ignite_fire + reset
│   └── waypoint_follower.py# SimpleWaypointFollower（替代 BasicAgent，spec D9）
├── obs/
│   ├── event_log.py        # 环形缓冲 + 订阅者
│   └── metrics.py          # tick_fps + command_bus.depth 等
├── config.py               # pydantic-settings + TOML 分层加载
└── main.py                 # 进程入口、装配、信号处理、shutdown 顺序

不存在的目录（曾经存在，refactor v0.3 已删）：
× carlabridge/agent/        # AgentLink / MockAgentLink / SocketIOAgentLink — 全删
```

**非 `carlabridge/` 的关键文件**：

| 文件 | 角色 |
|---|---|
| `test_agent.py` | 仓 root 的外部测试 Agent（Socket.IO 客户端），≤ 250 LOC，零 `carlabridge.*` import |
| `tests/` | 145 个单元测试，全部不依赖 CARLA |
| `tests/fakes/fake_world.py` | `World` / 相机 / 传感器最小子集，让 sim 域链路无 CARLA 也能跑 |
| `scripts/restart_smoke.ps1` | NF7 + AC-8 5 次启停验证 |
| `scripts/nf5_memory_probe.ps1` | NF5 30 分钟内存增长验证 |
| `config/default.toml` | 默认配置（dev-tune：state_hz=1 / fixed_delta_seconds=0.05） |
| `run.py` | 跨平台启动脚本（参数透传 + 启动前 POST `/admin/shutdown` 释放端口 + TIME_WAIT 预警） |

---

## 4. 关键数据流

### 4.1 视频流（每相机一路）

```
CARLA sensor cb (CARLA thread)
       │   raw RGBA buffer
       ▼
FrameQueue[cam_id].set_latest(frame)        ← 永不阻塞（drop_old）
       ▼
aiortc VideoStreamTrack[cam_id].recv()      ← async, awaits queue
       │   numpy → av.VideoFrame
       ▼
aiortc VP8 软编（线程池）→ WebRTC → 浏览器

# MJPEG 兜底（同 FrameQueue 分叉）：
GET /video_feed?camera=aerial → JPEG encode per frame → multipart write
```

### 4.2 状态流

```
TickLoop (sim domain):
  world.tick()
  snap = SnapshotBuilder.build(world, fleet, run_id, session_id, in_flight, frame=tick_count)
  snapshot_ref.set(snap)                     ← 原子引用替换

Broadcaster (async, 10 Hz):
  snap = snapshot_ref.get()
  fe_payload = projector.for_frontend(snap, focus_binding)
  ag_payload = projector.for_agent(snap)
  await sio.emit('state_update',   fe_payload,                 namespace='/')        # 裸
  await sio.emit('state_snapshot', envelope.wrap(...ag_payload),namespace='/agent')  # envelope
```

### 4.3 控制流（Agent → Bridge）—— 两阶段反馈

```
Agent: await sio.call('agent.command', envelope, namespace='/agent', timeout=2.0)
                  ▼
agent_ns.on_agent_command(sid, payload)
   ├─ unwrap(payload) → 协议 §3.2 兼容 envelope/裸两形态
   ├─ if scenario._resetting → return {rejected, reason:"scenario_resetting"}
   ├─ try parse_command(body)
   │    └─ RejectCommand → return {rejected, reason, detail}
   ├─ command_bus.submit(cmd)
   │    └─ 满 → return {rejected, reason:"overloaded"}
   └─ return {accepted, cmd_id, queued_at_sim_time}    ← sio.call 返回值

TickLoop (next tick):
  while not command_bus.empty():
    cmd = command_bus.get()
    try:
      scenario.on_command(cmd)                          ← _accept_command + supersede + dispatch
    except RejectCommand as r:
      command_bus.broadcast_command_status({...,status:"failed",reason:r.reason})

  # 每 tick 末扫 _in_flight:
  scenario._drive_command_lifecycle(sim_time)
    └─ for cmd in _in_flight:
         result = scenario._check_completion(cmd, sim_time)
         if result: scenario._finalize_command(cmd, result)
              └─ command_bus.broadcast_command_status(...)
              └─ event_log.add(..., cmd_id=...)
              ▼
         loop.call_soon_threadsafe(sio.start_background_task, agent_ns.broadcast_command_status, payload)
              ▼
         envelope.wrap('command_status', payload) → emit to all /agent sids
```

### 4.4 Operator → Bridge 控制流

```
Operator: POST /scenario/fire {position, ...}
              ▼
routes_scenario._fire(request)
   ├─ if runner.is_resetting() → 503 scenario_resetting
   ├─ validate body
   └─ await runner.run_in_sim_domain(scenario.ignite_fire, **kwargs)
              ▼
        投到 sim 队列的特殊任务，sim 域执行后 future.set_result
              ▼
        spawn fire actor + fleet.add_incident(Incident)
              ▼
        return 200 {incident_id, run_id, since_sim_time, ...}

下一帧 snapshot.incidents 包含新 incident → Agent 自然感知
```

`reset` 与 `fire` 同结构，区别在于 reset 进 sim 域执行 `teardown() → setup()`，全程置 `_resetting=True` 拦截命令；完成后 emit `scenario_event {event:"reset", run_id}` envelope。

---

## 5. 命令生命周期

8 条命令（协议 §6）落到 sim 域统一走两阶段：

```
                         (Agent: sio.call)
                                │
                                ▼
                         parse OK?   ─── No ──▶ rejected (终止)
                                │ Yes
                                ▼
                         accepted → _in_flight[cmd_id]
                                  → _in_flight_by_entity[target] = cmd_id
                                │
              ┌────────────── ┴────────────┐
              │                            │
        instant cmd                   long-running cmd
        (HOLD/STOP/                   (GOTO/RTL/PATROL/EXTINGUISH)
        EXTINGUISH 同 tick)               │
              │                            ├─ 期间被新命令替换 → cancelled(superseded)
              │                            ├─ 期间 reset      → cancelled(reset)
              │                            ├─ 期间执行报错    → failed(follower_error)
              │                            └─ 自然完成        → completed
              ▼                            │
        completed                          │
                                  ┌────────┴────────────┐
                                  │ PATROL loop=true:   │
                                  │ accept 后立即 emit  │
                                  │ ongoing 一次,       │
                                  │ 之后只可能 cancel   │
                                  │ 或 failed            │
                                  └─────────────────────┘
```

每 entity 同时只能有 1 条 in-flight；新命令到 `_accept_command` 时若发现 `_in_flight_by_entity[target]` 有旧 cmd，先 finalize 旧 cmd 为 `cancelled(superseded)` 再登记新 cmd。`UGV_STOP` 特殊：reason=`explicit_stop`。

完成检查（`_check_completion`，每 tick 扫一遍）：
- `UAV_GOTO/RTL`：UAV pose 距 target ≤ `UAV_ARRIVAL_EPS_M`（默认 0.5 m）
- `UAV_PATROL` (loop=false)：path index 走完
- `UAV_PATROL` (loop=true)：永远 None（accept 时已 emit ongoing）
- `UGV_GOTO/RTL`：`SimpleWaypointFollower.done()`
- `UGV_EXTINGUISH`：accept 时距离 check（≤ `EXTINGUISH_RADIUS_M`，默认 5 m）；`EXTINGUISH_DWELL_S`（默认 3 sim s）后 destroy fire actor + 移除 incident
- `UAV_HOLD` / `UGV_STOP`：标 `awaiting="instant"`，下一拍即完成

---

## 6. 相机绑定模型（M4 落定，refactor 不改）

通道接口固定为三个 ID：`aerial` / `ground` / `city`。每个通道在 `setup()` 时通过 `CameraManager.rebind(channel, entity_id)` 绑定。三种 mode：

| mode | sensor 怎么生成 | 跟随机制 |
|---|---|---|
| `attached_to_actor` | `world.spawn_actor(cam_bp, transform=offset, attach_to=actor)` | CARLA 内置 attach |
| `follows_virtual` | spawn 不 attach | `update_followers` 每 tick post 用虚拟实体 pose + offset 调 `sensor.set_transform()` |
| `world_pose` | spawn 不 attach | 一次性 set_transform |

S1 默认绑定：`aerial=follows_virtual UAV-01`、`ground=attached_to_actor UGV-01`、`city=world_pose (z=200, pitch=-90°)`。

**关键不变量（reset 友好）**：`CameraManager.rebind(channel, new_entity_id)` 销毁旧 sensor + spawn 新 sensor 但**保留同一个 FrameQueue 实例**。WebRTC track / MJPEG 流不重连，切换瞬间最多丢 < 100 ms 帧。

---

## 7. 协议合规（v1.0 envelope）

详见 `bridge-agent-protocol-v1.md`。本仓实现要点：

- **PROTOCOL_VERSION** = `"1.0"`，定义在 `carlabridge/bus/envelope.py`。
- **`/agent` 出站**所有事件包 envelope `{version, msg_id, type, timestamp, frame, sim_time, sender, payload}`（`bus/agent_ns.py` + `bus/broadcaster.py`）。
- **`/agent` 入站**（`hello` / `agent.command` / `event_log`）通过 `unwrap()` 同时容忍 envelope 与裸 dict。
- **RPC ack 不包 envelope**：`sio.call('agent.command', ...)` 返回 `{status, cmd_id, ...}` 简单 dict（协议 §5.1.2）；`hello` 返回值含 `{server, version:"1.0", bridge_session_id, scenario}`（协议 §2.2）。
- **`/`(前端) namespace 仍裸 payload**（避免破坏前端协议；前端协议不在 v1.0 范围）。
- **frame 注入**：`SimClock.tick_count` → `WorldSnapshot.frame` → envelope.frame（不进 payload）。

---

## 8. 配置

```toml
# config/default.toml（dev-tune 当前值）
[carla]
host = "127.0.0.1"
port = 2000
timeout_s = 30.0
fixed_delta_seconds = 0.05    # ≈ 20 Hz；演示机渲染瓶颈下从 0.0333 调高
map = "Town10HD_Opt"

[server]
host = "0.0.0.0"
port = 5000
cors_origins = ["http://localhost:5173"]

[broadcast]
state_hz = 1                  # dev-tune；目标值 10
metrics_hz = 1

[scenario]
default = "s1_fire"
```

覆盖优先级：env > `--config` overlay > `config/local.toml` > `config/default.toml` > pydantic 默认值。env 用 `CARLABRIDGE_` 前缀 + `__` 双下划线层级（如 `CARLABRIDGE_CARLA__PORT=2010`）。

代码内的 scenario 阈值（`UAV_ARRIVAL_EPS_M` / `EXTINGUISH_RADIUS_M` / `EXTINGUISH_DWELL_S` / `DEFAULT_UAV_RTL_SPEED` / `DEFAULT_UGV_TARGET_SPEED_KMH`）固化在 `carlabridge/scenarios/s1_fire.py`，当前不通过 toml 暴露（refactor v0.3 §7.7 列了 `[scenario.s1_fire]` 段，但代码尚未消费 toml 值；如果要暴露，加 4 行 pydantic 字段 + 一处构造时读取即可）。

---

## 9. 启停顺序

### 9.1 启动（`main.py:_run`）

1. `EventLog` + `Metrics` + `Fleet` + `bridge_session_id = "br-<uuid8>"`
2. （可选）`World.connect` → `save_original_settings` → `ensure_map` → `switch_to_sync(delta)`
3. `make_sio` + `CommandBus` + `build_app`（注册 frontend_ns / agent_ns + 路由）
4. 接 `command_bus.set_on_command_status` / `set_on_scenario_event` 回调（call_soon_threadsafe → start_background_task → namespace.broadcast_*）
5. `web.AppRunner.setup` + `TCPSite.start`（端口冲突 → 退出码 3）
6. `camera_manager.spawn_all`（city 立即起；aerial/ground 等 scenario 填 attach_entity_id）
7. `ScenarioRunner(...).start()` → `scenario.setup()`（spawn UGV、注册 UAVs、`fleet.set_origin`、rebind 相机）
8. `agent_ns.set_resetting_provider / set_sim_time_provider`（晚绑）
9. `TickLoop.start()`（独立线程）
10. `Broadcaster.start()`（10 Hz 异步任务）
11. `await stop_event.wait()`

### 9.2 关停（SIGINT / `POST /admin/shutdown` / `stop_event.set()`）

执行 `try/finally` 内的清理顺序，**任何启动失败也会走完**：
1. `broadcaster.stop()`
2. `shutdown_peer_connections()`（关 WebRTC + drain encoder）
3. `tick_loop.stop()` + `join(timeout=3s)`（停 sim 域，CARLA 不再被驱动）
4. `scenario_runner.stop()` → `scenario.teardown()`（destroy actors + unbind cameras）
5. `camera_manager.detach_all()`（兜底）
6. `runner.cleanup()`（HTTP/Socket.IO drain）
7. `world.restore_original_settings()` + `world.disconnect()`（关键：恢复 async mode，避免下次启动卡住）

**典型故障 & 自愈**：
- `OSError 10048` 端口占用 → 早期返回退出码 3，CARLA 已 restore（启动时 try/finally 包住了 `switch_to_sync` 之后的所有步骤）
- 异常退出（被 `Stop-Process -Force`）走不到 finally → CARLA 留 sync mode，需手动 reset（README §6.3 脚本）

---

## 10. 失败处理与背压

| 故障 | 检测 | 处置 |
|---|---|---|
| CARLA RPC 超时 | tick 函数 `try/except RuntimeError` | 累计 3 次失败 → 抛致命，触发 shutdown |
| sensor cb 异常 | per-callback `try/except` | 记 event_log warn，不传播到 CARLA 线程 |
| FrameQueue 持续满 | drop_counter 计数 | metrics 中 `dropped_frames`，每 5 s 一条 event_log |
| Command bus 满 | `submit()` return False | sio.call return `{rejected, reason:"overloaded"}` |
| `scenario.on_command` 抛 RejectCommand | tick_loop catch | broadcast `command_status:failed` + event_log |
| `_check_completion` 抛 | tick_loop catch | finalize 该 cmd 为 failed，不中断 scenario |
| Follower 崩溃 | `run_step()` 抛 | `_ugv_follower=None`，所有 awaiting=ugv_arrival 的 cmd 标 failed(follower_error) |
| reset 与并发命令竞争 | `_resetting` atomic bool | 全程拒新命令；当前 in-flight 一次性 cancelled(reset) |
| WebRTC ICE 失败 | aiortc 回调 | 关闭该会话，下次客户端重连 |
| Bridge 崩溃 | 无 | Agent 通过 `bridge_session_id` 重连后变化感知，清状态重发 PATROL |

**总原则**：sim 域永远不被 async 域拖慢；任何跨域投递都是非阻塞、允许丢弃。

---

## 11. 技术栈选型

| 关注点 | 选型 | 决议引用 |
|---|---|---|
| 异步 HTTP | aiohttp 3.x | 与 python-socketio + aiortc 同栈 |
| Socket.IO | python-socketio AsyncServer ≥ 5.x | 直接兼容前端 socket.io-client 4.x；sio.call return-value 形式 |
| WebRTC | aiortc（VP8 软编） | spec D5：简单优先 |
| 视频中转 | PyAV / numpy | aiortc 原生消费 av.VideoFrame |
| CARLA | carla 0.9.16 Python API（`carla==0.9.16`，PyPI 预编译轮子） | 与服务端锁定同版本 |
| UGV 导航 | `SimpleWaypointFollower`（工程内） | spec D9：BasicAgent 在 sync+camera 真机 30 s RPC timeout |
| 配置 | pydantic-settings | TOML + env 自动合并 |
| 日志 | stdlib logging | 不引入额外依赖 |
| 系统指标 | psutil + 自实现 tick_fps | — |
| 测试 | pytest + pytest-asyncio + python-socketio AsyncClient | — |

显式不选：FastAPI、ROS2、gRPC、Redis、GPU 编码、CARLA `BasicAgent` / `TrafficManager autopilot`。

---

## 12. HTTP / Socket.IO 端点

| 路径 | 协议 | 用途 | 实现位置 |
|---|---|---|---|
| `GET /healthz` | HTTP | 完整健康（carla / tick_fps / scenario / clients / cameras / metrics） | `bus/server.py` |
| `GET /debug/events?n=N` | HTTP | event_log 环形缓冲 dump | `bus/server.py` |
| `POST /admin/shutdown` | HTTP | 程序化优雅关停 | `bus/server.py` |
| `POST /webrtc/{cam_id}` | HTTP+SDP | WebRTC offer/answer | `streaming/webrtc.py` |
| `GET /video_feed?camera={id}` | MJPEG | 兜底视频 | `streaming/mjpeg.py` |
| `POST /scenario/fire` | HTTP JSON | operator 点火 | `bus/routes_scenario.py` |
| `POST /scenario/reset` | HTTP JSON | operator reset | `bus/routes_scenario.py` |
| `GET /scenario/status` | HTTP JSON | scenario 状态快照 | `bus/routes_scenario.py` |
| `/socket.io/` | Socket.IO | `/` (前端) + `/agent` (Agent) | `bus/{server,frontend_ns,agent_ns}.py` |

`/agent` namespace 事件清单（详见 protocol §4 / §5）：

| 方向 | 事件 | 形态 |
|---|---|---|
| → agent | `state_snapshot` | envelope, 10 Hz + connect 时单播 |
| → agent | `command_status` | envelope, 命令生命周期变化时 |
| → agent | `scenario_event` | envelope, 当前仅 `reset` |
| → agent | `event_log` | envelope, 人类可读 |
| ← agent | `hello` (RPC) | dict ack `{server, version, bridge_session_id, scenario}` |
| ← agent | `agent.command` (RPC) | dict ack `{accepted/rejected, cmd_id, ...}` |
| ← agent | `event_log` | 单向，source 强制覆盖为 "AGENT" |

---

## 13. 性能预算

| 项 | 预算 | 实测（演示机） |
|---|---|---|
| Tick 周期 | 33 ms (30 Hz) | ~250 ms (~4 Hz) — CARLA 渲染瓶颈，spec D5 已接受降级 |
| └ world.tick() | < 20 ms | ~110-300 ms |
| └ snapshot build | < 5 ms | OK |
| └ scenario on_tick_post | < 5 ms | OK |
| └ command drain | < 1 ms | OK |
| 状态广播单次 | < 10 ms | OK |
| WebRTC 端到端 | < 300 ms (NF2) | ✅ |
| 状态端到端 | < 100 ms (NF3) | ✅ |
| 命令端到端 | < 500 ms (NF4) | ✅ 实测 max 250 ms |
| 内存稳定 | < 800 MB（含 3 路 aiortc） | 待 nf5_memory_probe 跑 30 min |

NF1 30 Hz tick 在演示机上不可达；硬件升级才能达到。Bridge pacing 逻辑本身正确（FakeWorld 单测稳 30 Hz）。

---

## 14. 测试策略

| 层级 | 工具 | 覆盖 |
|---|---|---|
| 单元 | pytest | dispatcher / projector / snapshot / frame_queue / lifecycle / supersede 等 |
| 异步 | pytest-asyncio | broadcaster 节奏、命令 RPC |
| 集成（无 CARLA） | `tests/fakes/fake_world.py` | tick + scenario + broadcaster 闭环 |
| 端到端契约 | python-socketio AsyncClient | hello / agent.command / command_status / scenario_event |
| 端到端冒烟（有 CARLA） | `test_agent.py` + `POST /scenario/fire` | 见 README §3.5 |
| 长跑 NF | `scripts/nf5_memory_probe.ps1` / `restart_smoke.ps1` | NF5 / NF7 |

145 个单元测试，全部不需要 CARLA，~16 秒跑完。

---

## 15. 决议与与历史文档的映射

当前实现遵循以下决议（出处仍可在 archive 中查到，列在这里方便追溯）：

| 决议 | 含义 | 出处 |
|---|---|---|
| D1 | 灵活绑定推送（aerial/ground/city 接口固定，scenario 启动时绑实体） | `docs/archive/spec-v0.1.md` §12 |
| D3 | 防爆罐/机械臂本期不实现，UGV_EXTINGUISH 仅做距离判定 + actor destroy | 同上 |
| D5 | WebRTC VP8 软编 | 同上 |
| D6 | python-socketio + aiohttp 单进程同栈 | 同上 |
| D7 | city 用高空固定相机 | 同上 |
| D9 | UGV 用 SimpleWaypointFollower 替代 BasicAgent | 同上 |
| D10 | Bridge 时间无关 + 8 命令 + 通用生命周期 + UrbanAgent 完全外部 + HTTP 触发 | 同上（refactor 决议） |
| protocol v1.0 | envelope 包裹 + sio.call ack + hello version | `bridge-agent-protocol-v1.md` |
| R1~R11 | refactor 落地的 ~30 个具体任务 + DoD | `docs/archive/tasks-refactor-r1-r11.md` |

D2 / D4 / D8 / 旧版 §5.3 / §8 / §9（agent_ack/agent_reject、mock_agent、AgentLink）已**全部废弃**，不再适用于本实现。
