# CarlaBridge 架构设计（design.md）

> 文档目的：定义 **HOW** —— 模块结构、并发模型、数据流、技术栈。
> 上游文档：`spec.md`（WHAT / WHY，v0.1 已评审）。
> 下游文档：`tasks.md`（开发拆分，下一步生成）。

| 字段 | 值 |
|---|---|
| 版本 | v0.1 |
| 状态 | 草案，与 spec.md v0.1 配套 |
| 语言 / 运行时 | Python 3.12  环境已经配置在 conda D:/carla/env 中，window 10 单机  carla 0.9.16 |

---

## 1. 设计目标 / 非目标

### 目标
1. **CARLA tick 永不阻塞**：30 Hz 主循环抖动 < ±2 Hz（NF1）
2. **三层独立频率**：CARLA tick 30 Hz / 视频 25 fps（可配）/ 状态广播 10 Hz（可配）
3. **单一控制源（Agent）+ 只读前端**：架构上让前端的命令通道是「向 Agent 提议」语义，控制落地只走 Agent → Scenario 这一条路
4. **Mock / 真实 Agent 零差异**：两者实现同一个内部接口 `AgentLink`
5. **简单优先**：单进程、单 asyncio loop + 一条 tick 线程；不引入 Redis / ZMQ / 多进程

### 非目标
- 不追求 < 100ms 端到端延迟（spec 目标 < 300ms）
- 不追求 GPU 编码（D5：VP8 软编即可）
- 不做热加载、不做配置中心
- 不做认证、TLS

---

## 2. 高层架构

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              CarlaBridge (Python)                            │
│                                                                              │
│   ┌──────────────────┐                                                       │
│   │  Tick Thread     │       ┌──────────────────────────────────────────┐    │
│   │  (sim domain)    │       │       Asyncio Loop (async domain)        │    │
│   │                  │       │                                          │    │
│   │ ┌──────────────┐ │ ────► │ ┌────────────┐  ┌────────────────────┐   │    │
│   │ │ WorldClock   │ │ snap  │ │Broadcaster │  │ Socket.IO Server   │   │    │
│   │ │ Scenario     │ │ ref   │ │ (10 Hz)    │──┤   /  (frontend)    │◄──┼────┼── React 前端
│   │ │ Snapshot     │ │       │ └────────────┘  │   /agent           │◄──┼────┼── Urban Agent
│   │ │ Builder      │ │       │ ┌────────────┐  └────────────────────┘   │    │   (mock or real)
│   │ └──────────────┘ │       │ │ aiohttp    │  ┌────────────────────┐   │    │
│   │       ▲          │ ◄──── │ │ HTTP       │──┤ WebRTC signaling   │◄──┼────┼── 浏览器 PC
│   │       │ cmd      │  cmd  │ │ routes     │──┤ MJPEG /video_feed  │◄──┼────┤
│   │       │ queue    │ queue │ └────────────┘  └─────────┬──────────┘   │    │
│   └───────┼──────────┘       │                           │              │    │
│           │                  │       ┌───────────────────┴─────────┐    │    │
│           │                  │       │ aiortc VideoStreamTrack (×N) │   │    │
│           │                  │       └───────────────────▲─────────┘    │    │
│           │ sensor cb        │                           │              │    │
│           │ (CARLA threads)  │       ┌───────────────────┴─────────┐    │    │
│   ┌───────┴──────────────────┼───────┤ Frame Queue ×N (latest-1)   │    │    │
│   │  CARLA Sensors (Cam ×N)  │ frame └─────────────────────────────┘    │    │
│   └──────────────────────────┘                                          │    │
│                                                                         │    │
└──────────────────────────────────────────────────────────────────────────────┘
                   ▲
                   │ Python API
                   ▼
            ┌──────────────┐
            │ CARLA Server │
            └──────────────┘
```

**两个执行域**：
- **Sim domain**（单条专属线程）：拥有 CARLA 主循环，调用 `world.tick()`、构建 `WorldSnapshot`、运行 `scenario.on_tick()`
- **Async domain**（main thread 的 asyncio loop）：托管 aiohttp + python-socketio + aiortc，处理所有 I/O

**跨域只有三条通道**：
1. **WorldSnapshot 引用**（sim → async）：单值原子替换，无锁读
2. **Frame Queues**（sim → async，每相机一个）：bounded=1，latest-wins
3. **Command Queue**（async → sim）：`queue.Queue`，bounded=64

---

## 3. 进程 / 线程模型

| 线程 | 数量 | 职责 |
|---|---|---|
| **Main / Asyncio** | 1 | aiohttp、Socket.IO、aiortc、broadcaster、mock agent 协程 |
| **Tick Thread** | 1 | CARLA tick + 场景脚本 + 快照构建 |
| **CARLA sensor 内部线程** | 由 CARLA 控制 | 仅做 `frame_queue.put_latest()` 后立即返回 |
| **VP8 编码线程池** | aiortc 内部 | 视频帧编码，CPU 密集 |

**为什么不用单纯 asyncio？**
- CARLA Python API 的 `world.tick()` 是阻塞同步调用（毫秒级），且 sensor callback 来自 CARLA 自有线程
- 在 asyncio loop 里直接调 tick 会阻塞所有 I/O — 视频帧推送会卡顿
- 单独一条 tick 线程把同步世界与异步世界隔离

**为什么不用多进程？**
- 多进程要解决 numpy frame 跨进程传递（共享内存或序列化），复杂度高
- 单进程 + GIL 释放（VP8 编码、CARLA RPC、PyAV 都 release GIL）已经够用
- D5/D6：简单优先

---

## 4. 模块分解

```
carlabridge/
├── core/
│   ├── world.py            # CARLA 连接、sync mode 开关、actor 注册表
│   ├── tick_loop.py        # 30 Hz 节拍驱动（独立线程）
│   ├── snapshot.py         # WorldSnapshot dataclass + 构建器
│   ├── clock.py            # sim_time / wall_time
│   └── fleet.py            # UAV/UGV/TL 角色登记
├── sensors/
│   ├── camera.py           # 摄像头 spawn / detach
│   └── frame_queue.py      # 单元素「最新覆盖」队列
├── streaming/
│   ├── webrtc.py           # aiortc VideoStreamTrack 子类 + signaling 路由
│   ├── mjpeg.py            # multipart/x-mixed-replace 路由（兜底）
│   └── jpeg_tap.py         # 按需 JPEG 编码（仅 MJPEG 客户端在线时启用）
├── bus/
│   ├── server.py           # python-socketio AsyncServer + aiohttp app 装配
│   ├── frontend_ns.py      # `/` namespace（前端，只读）
│   ├── agent_ns.py         # `/agent` namespace（Agent，读写）
│   ├── broadcaster.py      # 10 Hz 状态扇出 + 投影
│   └── projector.py        # WorldSnapshot → 前端 state_update / Agent state_snapshot
├── agent/
│   ├── link.py             # AgentLink 抽象接口
│   ├── socketio_agent.py   # 真实 Agent 适配（连入 /agent namespace）
│   └── mock_agent.py       # 嵌入 scenario 的剧本驱动 mock
├── scenarios/
│   ├── base.py             # Scenario ABC + setup/on_tick/on_command/teardown
│   ├── s1_fire.py          # S1 火灾应急 + 写死的 mock 剧本
│   └── runner.py           # 场景生命周期管理
├── commands/
│   ├── enum.py             # 内部指令枚举（UAV_RTL / UGV_DISPATCH / ...）
│   └── dispatcher.py       # agent_command → scenario.on_command(parsed)
├── config.py               # pydantic Settings
├── obs/
│   ├── event_log.py        # event 环形缓冲 + 广播
│   └── metrics.py          # psutil + tick fps 采样
└── main.py                 # 进程入口、装配、信号处理
```

---

## 5. 关键数据流

### 5.1 视频流（每相机一路）

```
CARLA sensor cb (CARLA thread)
       │   raw RGBA buffer (numpy view)
       ▼
FrameQueue[cam_id].set_latest(frame)        ← put_nowait_drop_old
       │
       │ (无 GIL 等待，sensor cb 立即返回)
       ▼
aiortc VideoStreamTrack[cam_id].recv()      ← async, awaits queue
       │   numpy → av.VideoFrame
       ▼
aiortc 内部 VP8 编码线程池
       ▼
WebRTC → 浏览器
```

**MJPEG 兜底路径**（同源帧队列分叉）：
```
GET /video_feed?camera=aerial
       │
       ▼ aiohttp handler
FrameQueue[aerial].subscribe() → JPEG encode per frame → multipart write
```

### 5.2 状态流

```
Tick Thread:
  world.tick()
  snap = SnapshotBuilder.build(world, fleet)
  snapshot_ref.set(snap)            ← 原子引用替换

Async Loop (10 Hz broadcaster task):
  snap = snapshot_ref.get()
  fe_payload = Projector.for_frontend(snap, focus_binding)
  ag_payload = Projector.for_agent(snap)
  await sio.emit('state_update', fe_payload, namespace='/')
  await sio.emit('state_snapshot', ag_payload, namespace='/agent')
```

`snapshot_ref` 用 `contextvars.ContextVar` 或简单的 `threading.local` 都过度了 —— 直接用一个 `Atomic[Snapshot]` 包装：单变量赋值在 CPython 下天然原子，加 `volatile` 语义即可。

### 5.3 控制流（Agent → CARLA）

```
Agent (Socket.IO client on /agent)
       │  emit('agent_command', {id, target, text, payload})
       ▼
agent_ns.py handler (async)
       │
       │  cmd = CommandDispatcher.parse(payload)
       │  command_queue.put_nowait(cmd)              ← 跨域投递
       │
       ▼
Tick Thread (开始下一个 tick 前)
  while not command_queue.empty():
      cmd = command_queue.get_nowait()
      try:
          scenario.on_command(cmd)
          ack(cmd.id)                                ← call_soon_threadsafe
      except Reject as r:
          reject(cmd.id, r.reason)
```

ack/reject 通过 `loop.call_soon_threadsafe(sio.emit, ...)` 回到 async 域，再扇出到前端 + Agent。

### 5.4 前端「命令建议」路径

```
前端 emit('agent_command', ...)
       │
       ▼
frontend_ns.py handler
       │  event_log: "前端建议 → Agent"
       │  link.on_suggestion(payload)       ← 调 AgentLink，统一入口
       │
       ▼
AgentLink 实现：
  - MockAgentLink: 把 suggestion 投递到 mock_agent_loop 的内部建议队列，
                   loop 决定是否采纳。采纳 → emit_command；否则 emit_reject(reason)
  - SocketIOAgentLink: emit('suggestion', payload) 到远程 Agent（同前），
                   Agent 自行决定 ack/reject 回包
       │
       ▼
ack/reject 走与正常命令同一条下行路径（同时广播给前端 + agent namespace）
```

**为什么需要 `suggestion` 而不是直接复用 `agent_command`**：避免把「前端的建议」与「Agent 的决策」在事件流里混淆 —— Agent 收到的 `agent_command` 永远是它**自己**发出的回声（如有需要），而 `suggestion` 明确是「外部输入待评估」。前端契约不变，仅 Bridge 内部多一层语义。

---

## 6. 跨域通信原语

| 数据 | 方向 | 容器 | 满 / 空策略 |
|---|---|---|---|
| `WorldSnapshot` | sim → async | 单值 atomic ref | 永远只读最新 |
| `Frame[cam_id]` | sim → async | bounded=1 latest-wins | 旧帧立即丢弃，计数到 metrics |
| `Command` | async → sim | `queue.Queue(maxsize=64)` | 满则同步回 `agent_reject(reason="backpressure")` |
| `Event` | sim → async | `loop.call_soon_threadsafe` 直接广播 | event_log 环形缓冲在 async 域内 |
| `Ack/Reject` | sim → async | `loop.call_soon_threadsafe(sio.emit)` | 同上 |

**关键纪律**：sim 线程**绝不**直接调用任何 `sio.emit` / `await` —— 一律通过 `call_soon_threadsafe` 投递。

---

## 7. 摄像头绑定模型（实现 spec D1）

通道接口固定为三个 ID：`aerial` / `ground` / `city`。每个通道在 scenario `setup()` 时绑定，绑定类型有三种：

```python
class S1FireScenario(Scenario):
    def setup_bindings(self, fleet):
        return {
            # aerial: 跟随虚拟 UAV（无 CARLA actor），每 tick 由 scenario 推算虚拟 pose，camera 是独立高空 sensor，set_transform 跟过去
            "aerial": CameraBinding(mode="follows_virtual", target="UAV-01", offset=Pose(z=20, pitch=-30), fov=90, res=(1280, 720), fps=25),
            # ground: attach 到真实 CARLA vehicle actor，CARLA 原生跟随
            "ground": CameraBinding(mode="attached_to_actor", actor="UGV-01", offset=Pose(z=2, x=-3), fov=70, res=(1280, 720), fps=25),
            # city: 固定 world pose，spectator 高空俯视
            "city":   CameraBinding(mode="world_pose", world_pose=Pose(x=0, y=0, z=300, pitch=-90), fov=90, res=(1280, 720), fps=25),
        }
```

三种 mode 实现细节：

| mode | sensor 怎么生成 | 跟随机制 |
|---|---|---|
| `attached_to_actor` | `world.spawn_actor(cam_bp, transform=offset, attach_to=actor)` | CARLA 内置 attach，每 tick 自动跟随 |
| `follows_virtual` | `world.spawn_actor(cam_bp, transform=initial)` 不 attach | Bridge tick 末尾用虚拟实体 pose + offset 调 `sensor.set_transform(...)` |
| `world_pose` | 同上，不 attach | 一次性 set_transform，无需更新 |

**焦点切换（rebind）**：scenario 调用 `self.rebind("aerial", "UAV-02")`：
- `follows_virtual` / `attached_to_actor`：destroy 旧 sensor + spawn 新 sensor，**保留同一个 FrameQueue 实例**；WebRTC track 不重连
- `world_pose`：只改 pose，不 destroy
- 切换瞬间会丢失若干帧（< 100 ms），可接受

---

## 8. 场景引擎设计

### 8.1 Scenario 基类

```python
class Scenario(ABC):
    name: str

    def setup(self, world: World, fleet: Fleet) -> CameraBindings: ...
    def on_tick(self, snap: WorldSnapshot, sim_time: float) -> None: ...
    def on_command(self, cmd: ParsedCommand) -> None: ...   # 抛 Reject 表示拒绝
    def teardown(self) -> None: ...

    # 嵌入式 mock agent 入口（由 runner 在 async 域调用）
    async def mock_agent_loop(self, link: AgentLink) -> None: ...
```

### 8.2 S1 实现骨架

```python
class S1FireScenario(Scenario):
    SCRIPT = [
        ScriptEvent(at=6.0,  kind="detect_fire",       location=(...)),
        ScriptEvent(at=7.0,  kind="cmd",  target="UAV-02", text="UAV_RTL"),
        ScriptEvent(at=7.0,  kind="cmd",  target="UAV-03", text="UAV_RTL"),
        ScriptEvent(at=8.0,  kind="cmd",  target="UGV-01", text="UGV_DISPATCH", payload={"lat":..., "lng":...}),
        # ... 直到 ScriptEvent(at=..., kind="cmd", target="UGV-01", text="UGV_RTL")
    ]

    def setup(self, world, fleet):
        # spawn 3 UAVs, 1 UGV, traffic lights are world-native
        # spawn fire marker actor at SCRIPT[0].location
        return self.setup_bindings(fleet)

    async def mock_agent_loop(self, link):
        start = self.runner.sim_time()
        for ev in self.SCRIPT:
            await self.runner.sleep_until(start + ev.at)
            if ev.kind == "detect_fire":
                await link.emit_event_log("warn", "AGENT", f"detected fire at {ev.location}")
            elif ev.kind == "cmd":
                await link.emit_command(make_cmd(ev))
```

`mock_agent_loop` 跑在 async 域。它通过 `AgentLink` 调用产生的事件，与真实 Agent 经 Socket.IO 发出的事件**走同一条下游路径**（command_queue + event_log broadcaster），保证真实 Agent 替换 mock 时下游零修改。

### 8.3 Scenario Runner

- 全局唯一，持有当前 scenario 实例
- 启动：`scenario.setup()` → 启动 mock_agent_loop（async task）→ tick thread 开始驱动
- 停止：cancel mock_agent_loop → `scenario.teardown()` → 销毁 actor

### 8.4 实体实现细节

#### UAV — 虚拟实体
- 不 spawn 任何 CARLA actor
- `VirtualUav` dataclass：`id / pose / altitude / heading / battery / role / target`
- scenario 每 tick 推算运动（巡逻路径 / RTL 直线 / HOLD 悬停）：简单 lerp 即可
- snapshot 把虚拟 UAV 注入到 `WorldSnapshot.uavs`，与 CARLA 实体并行存在
- aerial 相机用 `follows_virtual` mode，每 tick 由 scenario hook 更新相机 transform

#### UGV — 真实 CARLA vehicle + SimpleWaypointFollower（M6 真机验收后落定）
- 用 `vehicle.lincoln.mkz_2020` 优先（fallback：lincoln.mkz_2017 / tesla.model3 / 其他 `vehicle.*`）。S1 spawn 时遍历**所有 spawn point × 所有候选蓝图**，第一个 `try_spawn_actor` 非 None 即锁定 anchor。
- **不用 `BasicAgent`** —— 真机验证发现它在 sync mode + 3 路 camera 环境下会出 30 s 级 RPC timeout（`bounding_box.extent.x` 等属性查询卡死）；同样代码在孤立测试里 2 ms 完成。推测是 camera 的 sensor listener 在 CARLA 内部线程持锁与 BasicAgent 的 `get_actors().filter()` / bounding_box 高频 RPC 累积冲突。spec D9 记录了这次决议。
- 落地实现：`carlabridge/scenarios/waypoint_follower.py:SimpleWaypointFollower`
  ```python
  follower = SimpleWaypointFollower(ugv_actor, target_speed_mps=25/3.6)
  follower.set_destination(world, carla.Location(x, y, z))  # 一次性 GRP 建路径
  # 每 tick post:
  ctrl = follower.run_step()
  ugv_actor.apply_control(ctrl)
  if follower.done(): event_log: arrived
  ```
- 每 tick 只 3 个 RPC：`get_transform / get_velocity / apply_control`。无避障、无红绿灯识别、无车道偏移——足够 S1 demo。
- 路径：`GlobalRoutePlanner(sampling_resolution=2.0).trace_route(start, end)` 返回 waypoint 序列，加上字面终点。
- 控制律：航向误差 P 控制（±45° 满舵），速度 P 控制（< target 油门 0.6，> target×1.2 刹车 0.3）。
- 到达检测：waypoint 进入半径 3 m → 推进下一个；终点半径 4 m → `done()` + 刹停。

#### 红绿灯 — CARLA world 原生
- 启动时 `world.get_actors().filter('traffic.traffic_light')` 全部入快照
- 每 tick 读 `light.state` + `light.get_red_time()/get_green_time()/get_yellow_time()` 算剩余
- 不进 Fleet（Bridge 不主动改其状态，本期只读）

#### 火灾标记 — D4 简化
- spawn 一个不可见的 `static.prop.streetbarrier` 或类似 actor 作为坐标锚点
- 不做火焰特效；event_log 中携带坐标即可
- 鸟瞰图上若有需要，前端可后续叠加 marker（不在本期）

---

## 9. Mock vs 真实 Agent

```
┌──────────────────────────┐
│       AgentLink          │  ← 抽象接口（async）
│ - emit_command(cmd)      │  ← Agent 向 CARLA 下发指令
│ - emit_event_log(...)    │  ← Agent 汇报决策原因
│ - on_state_snapshot(snap)│  ← Agent 收到状态时的钩子（可选）
│ - on_suggestion(cmd)     │  ← 前端建议进入 Agent 评估队列
└────────┬─────────────────┘
         │
   ┌─────┴───────────────────┐
   │                         │
   ▼                         ▼
MockAgentLink         SocketIOAgentLink
（in-process）        （桥接 /agent namespace 远程客户端）
```

- 配置开关 `agent.mode = "mock" | "remote"`
- `mock`：scenario.mock_agent_loop 收到一个 `MockAgentLink`，直接调内部 dispatcher
- `remote`：等待远程客户端 `hello`，把进来的 socketio 事件翻译成 `AgentLink` 调用

Broadcaster 在两种模式下都广播 `state_snapshot` 到 `/agent` namespace —— mock 模式下没人订阅也无所谓（python-socketio 对 0 订阅者扇出是 no-op）。

---

## 10. 协议与端口

单进程单端口（默认 `:5000`），aiohttp 同时挂载：

| 路径 | 协议 | 用途 |
|---|---|---|
| `/socket.io/` | Socket.IO（websocket transport） | 前端 + Agent namespace 复用 |
| `POST /webrtc/<cam_id>` | HTTP JSON | WebRTC SDP offer/answer |
| `GET /video_feed?camera=<cam_id>` | MJPEG | 兜底 |
| `GET /healthz` | HTTP | 健康检查（CARLA 连接、tick fps、订阅者数量） |

CORS：开发期允许 `http://localhost:5173`（前端 vite dev）。

---

## 11. 技术栈选型

| 关注点 | 选型 | 原因 / 决议引用 |
|---|---|---|
| 异步 HTTP | **aiohttp 3.x** | python-socketio 推荐组合，aiortc 也基于 aiohttp（D6） |
| Socket.IO | **python-socketio AsyncServer** | 直接兼容前端 `socket.io-client` 4.x |
| WebRTC | **aiortc** | 纯 Python，VP8 软编内置（D5） |
| 视频中转 | **PyAV / numpy** | aiortc 原生使用 av.VideoFrame |
| CARLA | **carla 0.9.16 Python API** | 项目锁定版本，sync mode 已稳定 |
| UGV 导航 | **`SimpleWaypointFollower`**（工程内） | M6 真机验证 `BasicAgent` 在 sync+camera 环境 RPC timeout 30 s；follower 每 tick 仅 3 个 RPC，GRP 一次性建路径。spec D9 |
| 配置 | **pydantic-settings** | TOML/env 自动合并，类型校验 |
| 日志 | **stdlib logging + JSON formatter** | 不引入额外依赖 |
| 系统指标 | **psutil** | CPU/MEM/NET；GPU 通过 `nvidia-smi` 子进程（可选） |
| 测试 | **pytest + pytest-asyncio** | 单元 + 集成 |
| 进程管理 | 启动脚本 `run.ps1` + `Ctrl+C` | 单机演示足够 |

**显式不选**：
- ❌ FastAPI（与 python-socketio 集成较曲折，aiohttp 更直接）
- ❌ ROS2（spec §2.3 不在范围）
- ❌ gRPC / ZMQ（spec §12 D6 简单优先）
- ❌ Redis / Kafka（单机不需要持久化扇出）
- ❌ GPU 编码（D5）
- ❌ CARLA `BasicAgent` / `TrafficManager autopilot`（D9：BasicAgent 真机 timeout；autopilot 不能定向目的地）

---

## 12. 配置模型

```toml
# config/default.toml
[carla]
host = "127.0.0.1"
port = 2000
timeout_s = 10.0
fixed_delta_seconds = 0.0333    # 30 Hz
map = "Town10HD_Opt"            # 启动时检查/加载

[server]
host = "0.0.0.0"
port = 5000
cors_origins = ["http://localhost:5173"]

[broadcast]
state_hz = 10
metrics_hz = 1

[video]
default_fps = 25
default_resolution = [1280, 720]
frame_queue_drop_log_interval_s = 5

[agent]
mode = "mock"                   # "mock" | "remote"

[scenario]
default = "s1_fire"

[logging]
level = "INFO"
event_log_buffer = 1000
```

env vars 覆盖：`CARLABRIDGE_CARLA__HOST=...` 风格（pydantic 默认支持双下划线层级）。

---

## 13. 生命周期

### 13.1 启动顺序

```
1. load_config()
2. world = World.connect(cfg.carla)         # 失败立即退出
3. world.save_original_settings()
3a. if world.get_map().name != cfg.carla.map: world.load_world(cfg.carla.map)   # Town10HD_Opt 检查/加载
4. world.switch_to_sync(delta=cfg.carla.fixed_delta_seconds)
5. app = build_aiohttp_app()
6. sio = build_socketio(app)
7. asyncio.create_task(broadcaster.run())
8. asyncio.create_task(metrics.run())
9. tick_thread = Thread(target=tick_loop.run, daemon=False).start()
10. scenario = ScenarioRegistry.get(cfg.scenario.default)
11. await runner.start(scenario)            # spawn actors + mock task
12. await aiohttp.serve(app)                # 阻塞主线程，asyncio loop 运行
```

### 13.2 关闭顺序（SIGINT / Ctrl+C）

```
1. signal.SIGINT handler: 设置 shutdown_event
2. runner.stop():
   - cancel mock_agent_task
   - scenario.teardown() (sim 域里执行，下一个 tick 间隙)
3. tick_loop 检测到 shutdown_event → 退出循环
4. tick_thread.join(timeout=3s)
5. aiohttp graceful shutdown（关闭所有 socket.io / webrtc 会话）
6. world.restore_original_settings()        # 关键：恢复 async mode
7. world.disconnect()
```

异常路径：任何步骤抛错都走 `try/finally` 进入步骤 6/7，确保 CARLA 状态干净。

---

## 14. 失败处理与背压

| 故障 | 检测 | 处置 |
|---|---|---|
| CARLA RPC 超时 | tick 函数 `try/except RuntimeError` | 累计 3 次失败 → 抛出致命，触发 shutdown |
| sensor cb 异常 | per-callback try/except | 记 event_log warn，不传播到 CARLA 线程 |
| FrameQueue 持续满 | drop_counter > N/s | event_log warn(BRIDGE)，metrics 中 `dropped_frames` 字段 |
| Command queue 满 | put_nowait → QueueFull | 同步返回 `agent_reject(reason="overloaded")` |
| Scenario.on_command 抛错 | catch in tick_loop | reject + 写 event_log，不中断场景 |
| Scenario.on_tick 抛错 | catch in tick_loop | event_log danger + `runner.fail()` → teardown |
| WebRTC ICE 失败 | aiortc 回调 | 关闭该会话，下次客户端重连 |
| Socket.IO 客户端掉线 | sio.on('disconnect') | 清理 sid 绑定，不影响其他客户端 |
| Mock agent 协程异常 | task done callback | event_log danger，可选自动重启 |

**背压总原则**：sim 域永远不被 async 域拖慢。任何跨域投递都是非阻塞、允许丢弃。

---

## 15. 日志与可观测性

### 15.1 event_log（业务事件）
- 环形缓冲，容量可配，默认 1000 条
- 新客户端连入时回放最近 N 条
- 字段：`{ts, severity, source, message}`，`source ∈ {BRIDGE, SCENARIO, AGENT, CARLA}`

### 15.2 system_metrics
- 1 Hz 采样：CPU%（psutil）、MEM%、NET（KB/s）、tick FPS（最近 1s 均值）
- 累计指标：dropped_frames、command_queue_high_watermark、events_per_minute

### 15.3 日志（开发用）
- 文件 `logs/bridge.log` + 控制台
- 关键路径用结构化 JSON：tick 周期、scenario 阶段、command 处理

### 15.4 健康检查
- `GET /healthz` 返回：
```json
{
  "carla": "connected",
  "tick_fps": 29.8,
  "scenario": "s1_fire/running",
  "clients": {"frontend": 1, "agent": 1},
  "cameras": {"aerial": "ok", "ground": "ok", "city": "ok"}
}
```

---

## 16. 测试策略

| 层级 | 工具 | 覆盖 |
|---|---|---|
| 单元 | pytest | SnapshotBuilder、Projector、CommandDispatcher、Scenario 状态机 |
| 异步 | pytest-asyncio | broadcaster 节奏、AgentLink 双实现互换 |
| 集成（无 CARLA） | 注入 FakeWorld stub | tick loop + scenario + broadcaster 闭环 |
| 端到端（有 CARLA） | 启动真 CARLA，跑 S1 5 次 | NF 全部验证 |
| 契约 | 录制 Socket.IO 流量比对前端期望事件 | AC-1 ~ AC-4 |

**不在测试范围**：UI 自动化（前端已有 mock 模式可独立跑）、性能压测（NF 用观察法验证）。

---

## 17. 性能预算

| 项 | 预算 | 备注 |
|---|---|---|
| Tick 周期 | 33.3 ms | 30 Hz |
| └ world.tick() | < 20 ms | CARLA 内部 |
| └ Snapshot build | < 5 ms | 纯 Python 数据拷贝 |
| └ Scenario on_tick | < 5 ms | 留余量 |
| └ Command drain | < 1 ms | bounded=64，常态 0~2 条 |
| 状态广播单次 | < 10 ms | 投影 + JSON 序列化 + emit |
| 视频帧编码（VP8 720p） | < 20 ms | aiortc 内部线程，不计入 tick |
| WebRTC 端到端延迟 | < 300 ms | NF2 |
| 状态端到端延迟 | < 100 ms | NF3 |
| 内存稳定基线 | < 800 MB | 含 aiortc + 3 路相机 |

预算超标的兜底：tick 频率降级到 20 Hz（运行时不可改，需重启）。

---

## 18. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| aiortc 在 Windows 下 VP8 性能不足 | 中 | 视频卡顿 | 降帧率到 15 fps；或换 H.264（需 ffmpeg 二进制） |
| CARLA Python API 在多线程下偶发死锁 | 中 | tick 卡死 | 所有 CARLA 调用集中在 tick 线程，sensor cb 仅入队 |
| python-socketio + aiohttp 与 aiortc 共享 loop 偶发冲突 | 低 | 信令失败 | 监控 ICE 状态，必要时把 aiortc 放到独立 loop 的辅线程 |
| Scenario 写死剧本与 sim_time 漂移 | 低 | 演示节奏跑偏 | mock_agent_loop 用 sim_time 而非 wall_time 触发 |
| 前端单焦点 vs 多 UAV 在演示中切换不清晰 | 中 | UX 差 | Scenario 在 rebind 时同步 event_log，前端可见焦点切换 |
| ~~BasicAgent 路径规划在 Town10HD 失败 / 卡死~~ | ~~中~~ | ~~UGV 不动~~ | ~~scenario 加超时（30s 无进度 → fail + reject）；备选写最简 waypoint follower~~ |
| **已落地 (M6)**: BasicAgent 在 sync+camera 真机 30s RPC timeout | — | UGV 不动 | spec D9 决议：永久切到 `SimpleWaypointFollower`（per-tick 仅 3 RPC，GRP 一次性建路）。详见 §8.4 UGV 实现。 |
| 虚拟 UAV pose 与 aerial camera 不同步 | 低 | 画面抖动 | scenario `on_tick_post` 先更新虚拟 pose、再触发 camera.set_transform，在同一 tick 完成 |
| state_snapshot 全量推送（Town10HD ~30 红绿灯 + 数十车辆）数据量过大 | 低 | 网络/CPU 压力 | 配置开关 `agent.snapshot_filter`：默认全量，必要时按距离裁剪 |

---

## 19. 与 spec.md 的映射关系

| spec 条目 | design 落实位置 |
|---|---|
| F1 CARLA 节拍主控 | §3 Tick Thread + §4 `core/tick_loop.py` |
| F2 视频流 | §5.1 + §4 `streaming/` |
| F3 状态数据 | §5.2 + §4 `bus/broadcaster.py` + `projector.py` |
| F4 Agent 接口 | §9 AgentLink 抽象 + §4 `agent/` |
| F5 摄像头管理 | §7 绑定模型 + §4 `sensors/camera.py` |
| F6 场景编排 | §8 + §4 `scenarios/` |
| NF1 tick 稳定度 | §3 独立线程 + §17 预算 |
| NF2/NF3 端到端延迟 | §6 跨域原语 + §17 预算 |
| NF6 背压容忍 | §6 + §14 |
| D1 灵活绑定 | §7 |
| D2 mock 写死 | §8.2 SCRIPT 列表 |
| D3 不实现机械臂 | scenario 仅推 event_log |
| D5/D6 简单优先 | §11 选型 |
| D7 鸟瞰简单实现 | §7 city 通道用 spectator |

---

## 20. 待 tasks.md 拆分的开发顺序（预告）

为方便下一步排期，先列出建议的实现里程碑（非本文档承诺）：

1. **M0 骨架**：config + main + 空的 aiohttp/socketio 起服务 + `/healthz`
2. **M1 CARLA 连接 + tick**：World + TickLoop + 干跑（无 scenario）
3. **M2 Snapshot + 状态广播**：到此前端 LIVE 状态栏应该亮
4. **M3 单路相机 + WebRTC**：先打通 city 通道
5. **M4 多路相机 + 绑定模型**：aerial / ground 接入
6. **M5 Scenario 引擎 + S1 spawn**：actors 出现，但还没事件
7. **M6 内部命令 + AgentLink + Mock Agent**：S1 端到端跑通
8. **M7 MJPEG 兜底 + healthz 完善 + event_log 持久缓冲**
9. **M8 验收 NF 项 + 文档**

具体粒度与依赖关系留给 `tasks.md`。

---

## 21. 术语补充（spec §13 之外）

| 术语 | 含义 |
|---|---|
| **Sim domain / Async domain** | 本设计里两个执行域的简称 |
| **AgentLink** | mock/真实 Agent 共同实现的内部接口 |
| **FocusBinding** | 三个前端通道（aerial/ground/city）当前绑定的 actor 映射 |
| **FrameQueue** | 单元素 latest-wins 队列，sim → async 视频帧通道 |
| **Projector** | WorldSnapshot 到前端 / Agent 投影的纯函数 |
