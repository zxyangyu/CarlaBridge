# CarlaBridge 需求规范 (spec.md)

> 文档目的：定义 **WHAT / WHY**，不涉及代码与实现细节。
> 配套文档：`design.md`（架构与技术选型）、`tasks.md`（开发拆分）将在本规范确认后生成。

| 字段 | 值 |
|---|---|
| 项目名 | CarlaBridge |
| 工作目录 | `D:\CarlaBridge` |
| 上游 | CARLA Simulator（Python API） |
| 下游 | `D:\urban_frontend`（React + Socket.IO + WebRTC 数字孪生指挥前端，**只读展示**） |
| 平行模块 | **Urban Agent**（单一调度系统，代替人工统一调度所有 UAV/UGV/交通，本期用按剧本 mock 替代） |
| 版本 | v0.1 — 初版需求 |
| 状态 | 草案，待评审 |

---

## 1. 目标与价值（WHY）

### 1.1 业务目标
搭建一座位于 **CARLA 仿真世界**、**前端指挥中心**与**Urban Agent 调度系统**之间的实时数据中间件，使得：

1. 前端可以**像观察真实城市一样**实时观察 CARLA 中正在发生的事情（视频 + 状态），**仅作展示，不参与控制**。
2. Urban Agent 是**唯一的控制源**，代替人工对所有 UAV / UGV / 交通要素进行统一调度；它从 CarlaBridge 持续接收状态快照，下发控制指令。
3. CARLA 的仿真节拍、传感器采集、视频编码、状态分发**互不阻塞**，整套系统能稳定运行 30 分钟以上不掉帧、不积压。
4. 火灾应急、管控区抓捕等**演示级场景**可以通过脚本一键复现，便于路演与验收。

### 1.2 为什么需要中间件而不是直接前后端对接
- CARLA Python API 是**同步、单线程**的，sensor callback 在主循环内执行；任何 I/O 阻塞都会拖慢全局 tick。
- 前端与 Agent 的协议（Socket.IO / WebRTC / JSON）与 CARLA 的协议（RPC + numpy buffer）**阻抗不匹配**。
- 同一份仿真状态需要扇出给两类消费者：**前端 N 个浏览器（只看）** 和 **Agent 1 个进程（看 + 控）**。
- 场景流程需要被**统一节拍编排**，而不是写死在 CARLA 启动脚本里。

CarlaBridge 承担：**节拍主控 + 异步采集 + 协议转换 + 扇出广播 + 场景编排 + Agent 控制指令落地**。

---

## 2. 范围

### 2.1 本期纳入（M0–M8）
- CARLA synchronous mode 主循环托管
- 多路摄像头视频流（WebRTC 主、MJPEG 兜底）
- 仿真状态广播（Socket.IO）
- Agent 双向通道（Socket.IO + 与前端共享同一服务器进程）
- 主场景「火灾应急」端到端跑通（流程 1–10）
- 摄像头清单**场景启动前预声明**，运行中不变；通道接口固定（aerial / ground / city），**绑定的具体 actor 可在 scenario 配置时更换**
- 状态采集对象明确包含：**红绿灯（traffic light）、车辆（含 UGV）、无人机（UAV）**，全部推送给 Agent
- Agent 由 **mock 模块按写死的剧本时间表** 输出识别结果与控制指令

### 2.2 后续扩展（非本期，但要为之预留接口）
- 增强场景「管控区抓捕」（流程 6′–10″）
- 运行时动态增删摄像头
- 真实 Urban Agent 调度系统替换 mock
- UE4 Pixel Streaming 替代鸟瞰摄像头（前端已留 `UE4 PIXEL STREAM` 徽标）
- Scenario 由 YAML/DSL 描述（当前用 Python 脚本）
- 防爆罐 / 机械臂动作的视觉化实现

### 2.3 不在范围
- CARLA 本身的安装、地图制作、车辆模型
- 前端 UI 修改（前端契约只读对齐，不反向修改）
- Agent 内部算法实现
- 用户认证、TLS、生产级部署
- 数据持久化、回放、录制（仅日志事件持久化）

---

## 3. 角色与利益相关者

| 角色 | 关注点 |
|---|---|
| **演示操作员** | 一键启动场景、看到画面流畅、Agent 决策可视化 |
| **Urban Agent 开发者** | 拿到稳定的城市状态流（红绿灯/车辆/UAV）和清晰的控制接口，能独立调试调度算法 |
| **前端开发者** | 后端契约稳定，字段命名与现有 `src/types/index.ts` 对齐 |
| **集成验收方** | 端到端流程可复现、关键指标可观测 |

---

## 4. 用户场景

### 4.1 主场景 S1 —「无人机巡逻 + 火灾应急」

| # | 触发 / 状态 | CarlaBridge 行为 | Agent 行为 | 前端可见 |
|---|---|---|---|---|
| 1 | 操作员点「启动 S1」 | 装载场景脚本；**注册 3 架虚拟 UAV**（Bridge 内部数据实体，非 CARLA actor）；spawn 1 辆 UGV；建立摄像头 | — | 三路画面就绪，状态栏 LIVE |
| 2 | UAV 群升空巡逻 | 推送 UAV pose + 巡逻摄像头视频 | — | UAV-01/02/03 高度/航向跳变 |
| 3 | 仿真时间到 `t_fire` | 在预设坐标 spawn「火灾标记」actor（带火焰贴图） | — | 鸟瞰图可见火源 |
| 4 | 持续推送图像与状态 | — | mock Agent 在 `t_fire+Δ` 按剧本输出，发 `event_log: detected=fire, location=(x,y)` | EventLog 出现红色告警 |
| 5 | — | — | mock Agent 按剧本发 `agent_command: target=UAV-02, text=RTL` 和 `target=UAV-03, text=RTL` | UAV 状态变化 |
| 6 | 收到 Agent RTL 指令 | 调用 CARLA API 让 UAV-02/03 返航；保留 UAV-01 跟进 | — | 两架降落、一架悬停火场 |
| 7 | — | — | mock Agent 按剧本发 `agent_command: target=UGV-01, text=DISPATCH, payload={lat,lng}` | UGV 出发 |
| 8 | 收到 DISPATCH 指令 | 调用 CARLA autopilot/route，UGV 沿规划路径到火源 | — | UGV 视角画面前进 |
| 9 | UGV 到达火源 | 标记到位事件；**本期不实现真实机械臂/防爆罐动作**，仅推 `event_log: arrived` | — | EventLog 提示「就位」 |
| 10 | — | — | mock Agent 按剧本（延迟 N 秒）发 `event_log: fire=extinguished` + `agent_command: target=UGV-01, text=RTL` | EventLog 转绿，UGV 返回 |
| 11 | UGV 返回原位 | 场景标记完成 | — | 状态栏「场景完成」 |

### 4.2 增强场景 S2 —「管控区行人抓捕」（接口预留，本期不实现）

流程 6′–10″ 与 S1 共享相同的 CarlaBridge 能力（spawn / control / 状态广播 / Agent 命令分发），所需新增能力：
- spawn 行人 actor 并在管控区移动
- UGV 与 UAV 之间的「协同导引」（UAV 持续推坐标，UGV 跟踪订阅）
- 「抓捕」用 attach + 动画模拟

> **设计含义**：S1 的 Agent 指令集（`DISPATCH / RTL / TRACK` 等）必须是**可扩展枚举**，而不是硬编码 if/else。

---

## 5. 功能需求（WHAT）

### F1. CARLA 节拍主控
- F1.1 启动时由 CarlaBridge 将 CARLA world 切换到 synchronous mode，固定 `fixed_delta_seconds`（默认 ~0.0333s = 30 Hz）。
- F1.2 `world.tick()` 只能由 CarlaBridge 内部的「Tick Loop」线程调用，**任何其他模块不得直接调用**。
- F1.3 Tick Loop 必须保证：sensor callback 中不做编码 / 网络 / 磁盘 IO，只做原始 buffer 入队。
- F1.4 提供 `pause / resume / step` 控制接口（仅本地命令行或调试用，不暴露给前端 UI）。
- F1.5 退出时必须将 CARLA 恢复 asynchronous mode 并销毁 spawn 的所有 actor。

### F2. 视频流
- F2.1 主协议 **WebRTC**，签约接口对齐前端 `src/services/webrtc.ts`：
  - `POST /webrtc/<camera_id>`，请求体 `{sdp, type:"offer"}`，响应 `{sdp, type:"answer"}`
- F2.2 兜底协议 **MJPEG**：`GET /video_feed?camera=<camera_id>`（前端 `VideoSurface.tsx` 已支持）。
- F2.3 视频帧采集与编码运行在**独立线程或异步任务**，sensor callback 仅把 raw frame 投递到有界队列。
- F2.4 单路摄像头帧率默认 25 fps，**可配置**，可独立于 CARLA tick 频率（允许丢帧降采样）。
- F2.5 队列满时采用**最新优先**策略（丢弃旧帧），不阻塞 sensor callback。
- F2.6 摄像头掉线 / 编码失败时，前端通过 WebRTC ICE state 变化或 MJPEG 404 自动降级（前端已实现）。

### F3. 状态数据
- F3.1 协议 **Socket.IO over WebSocket**（前端用 `socket.io-client`，CarlaBridge 必须作为 Socket.IO 服务端）。
- F3.2 CarlaBridge 持续从 CARLA 采集以下状态对象，作为统一的「世界快照」内部模型：
  - **红绿灯（traffic light）** — 每个路口信号灯的位置、相位（红/黄/绿）、剩余时间
  - **车辆（vehicle）** — 含 UGV 与城市背景车辆，id / pose / speed / heading / 角色（dispatchable / civilian）
  - **无人机（UAV）** — id / pose / altitude / heading / battery / 角色（patrol / follow / standby）
  - **聚合统计** — vehicles count / pedestrians count / aqi / alerts，供前端 `city.*` 字段使用
- F3.3 前端订阅（CarlaBridge → 前端，只读）：
  - `state_update` — 整体仿真状态快照 / 增量（按前端契约 §7.1 投影出 `uav / ugv / city`）
  - `system_metrics` — Bridge 自身 CPU/GPU/mem/net/fps
  - `event_log` — 文本事件（场景里程碑、告警、Agent 决策）
  - `agent_ack` — Agent 已接受指令（用于前端可视化指令链路）
  - `agent_reject` — Agent 拒绝指令
- F3.4 前端**不发起控制**。前端契约里保留的 `agent_command` 输入框本期定位为「向 Agent 提交建议」：CarlaBridge 收到后**写 event_log + 以 `suggestion` 事件透传给 Agent**（mock 或真实）；Agent 可选择性采纳，采纳则按正常 `agent_command` 流程下发，不采纳则回 `agent_reject(reason)`。前端收到 ack/reject 用于可视化指令链路。
- F3.5 状态广播频率默认 10 Hz，**可配置**，独立于视频和 CARLA tick。
- F3.6 状态采集与广播解耦：tick 线程写入「最新世界快照」，广播线程定时读取、按消费者（前端 / Agent）投影后扇出。
- F3.7 多前端客户端连接时，所有事件**广播**到全部订阅者；客户端首次连接时推送一份全量快照。

### F4. Agent 接口
- F4.1 Agent 是**单一的外部进程**（urban agent 调度系统），通过 Socket.IO 客户端连入 CarlaBridge，使用独立 namespace（例如 `/agent`）与前端区分。
- F4.2 CarlaBridge → Agent（决策输入，比前端粒度更细）：
  - `state_snapshot` — 完整世界快照：所有红绿灯 + 所有车辆（含 UGV）+ 所有 UAV，**不做投影裁剪**
  - `event_log` 关键事件（场景阶段切换、actor 生成/销毁）
  - 默认推送频率 10 Hz，**可配置**
- F4.3 Agent → CarlaBridge（控制输出）：
  - `agent_command` — 字段复用前端契约结构 `{id, target, priority, text}`，target 为具体 actor id（UAV-01 / UGV-01 / TL-12 …）
  - `event_log` — Agent 主动汇报识别结果、决策原因，severity / source 字段同前端契约
- F4.4 CarlaBridge 收到 `agent_command` 后：
  - 解析为内部「场景指令」枚举（见 F6.3）
  - 转发到当前运行的 scenario 执行
  - 执行结果以 `agent_ack` 或 `agent_reject` 同时广播给 Agent 与前端
- F4.5 **本期 Agent 由 mock 模块实现**：按写死的剧本时间表（嵌在 scenario 内）输出 `event_log` 与 `agent_command`，模拟真实调度系统的输出。真实 Agent 接入时**接口零修改**，只替换 mock 实现。
- F4.6 始终假设**单一 Agent**模型；mock 与真实 Agent 互斥接入，两者切换时接口零修改。

### F5. 摄像头管理
- F5.1 每个 scenario 在脚本里**声明一份摄像头清单**：
  ```
  cameras = [
    {id: "city",   type: "world_pose",      pose: ..., resolution: 1280x720, fps: 25},
    {id: "aerial", type: "follows_virtual", target: "UAV-01", offset: ..., ...},  # UAV-01 是虚拟实体，pose 每 tick 更新
    {id: "ground", type: "attached_to_actor", actor: "UGV-01", offset: ..., ...}, # UGV-01 是真实 CARLA actor
  ]
  ```
- F5.2 摄像头 ID 与前端 `VideoSurface` 的 `variant`（`aerial` / `ground` / `city`）对齐；scenario 必须至少提供这三个 ID，缺失则前端显示 placeholder。
- F5.3 跟随摄像头（aerial/ground）随绑定 actor 自动移动。
- F5.4 鸟瞰摄像头（city）使用 CARLA spectator 视角或高空固定相机模拟。
- F5.5 摄像头数量上限 6（本期），架构上不阻挡未来动态增删，但本期不开放运行时 API。

### F6. 场景编排
- F6.1 场景定义形式：**Python scenario 脚本类**，继承统一基类，实现 `setup() / on_tick() / on_command() / teardown()`。
- F6.2 场景生命周期由 CarlaBridge 调度，**不允许 scenario 自行调用 `world.tick()`**。
- F6.3 支持的内部指令枚举（v0.1）：
  - `UAV_RTL` — 指定 UAV 返航
  - `UAV_HOLD` — 悬停跟进
  - `UGV_DISPATCH` — 派遣 UGV 到坐标
  - `UGV_RTL` — UGV 返航
  - `ATTACH_ACTOR` — 给 actor 附加道具（防爆罐 / 机械臂）
  - `MARK_EVENT` — 推送 event_log
- F6.4 启动接口：本期可通过启动参数或本地 CLI 选择场景，前端**不**直接控制场景启停（避免误触发）。
- F6.5 失败 / 异常时场景必须能 graceful teardown（销毁 spawn、恢复 CARLA 模式）。

---

## 6. 非功能需求

| 编号 | 项 | 目标 | 验收方式 |
|---|---|---|---|
| NF1 | CARLA tick 稳定度 | 30 Hz 目标频率，连续 30 分钟抖动 < ±2 Hz | `system_metrics.tick_fps` 时序观测 |
| NF2 | 视频端到端延迟 | CARLA 渲染 → 浏览器显示 < 300 ms（单机回环） | 屏幕戳记法测量 |
| NF3 | 状态广播延迟 | tick → 前端 store 更新 < 100 ms | 注入时间戳对比 |
| NF4 | 指令端到端响应 | 前端发出指令 → 收到 `agent_ack` 或 `agent_reject` < 500 ms（mock 模式下含 mock 决策延时） | `latency_ms` 字段 |
| NF5 | 内存稳定性 | 30 分钟运行内存增长 < 200 MB | 进程监控 |
| NF6 | 背压容忍 | 模拟下游慢 1s 不导致 CARLA tick 阻塞 > 1 个 tick 周期 | 注入延迟测试 |
| NF7 | 启停可靠性 | 启动 → 跑完 S1 → 关闭，连续 5 次无残留 actor / 端口占用 | 自动化脚本 |
| NF8 | 单机部署 | 全部模块运行在同一台 Windows 机器，端口可配置 | 配置文件 + 启动脚本 |

---

## 7. 外部接口契约

### 7.1 与前端（强约束，对齐 `D:\urban_frontend\src\`）

#### 7.1.1 视频
| 端点 | 协议 | 说明 |
|---|---|---|
| `POST /webrtc/<camera_id>` | HTTP + SDP | WebRTC 一次性 offer/answer 交换，对齐 `src/services/webrtc.ts` |
| `GET /video_feed?camera=<camera_id>` | MJPEG | 兜底，对齐 `VideoSurface.tsx` mjpeg 模式 |

`camera_id` 至少包含：`aerial` / `ground` / `city`。

#### 7.1.2 Socket.IO（命名空间 `/`，前端默认 namespace）

| 方向 | 事件 | 载荷 | 备注 |
|---|---|---|---|
| ← server | `state_update` | `{uav?, ugv?, city?}` | 字段结构见 §8 |
| ← server | `system_metrics` | `{cpu, gpu, mem, net, fps}` | `cpu/gpu/mem/net` 为 0–100 数值；`fps` 字段语义 = **CARLA tick fps**（非浏览器渲染 fps） |
| ← server | `event_log` | `{severity, source, message}` | severity = `info`/`ok`/`warn`/`danger` |
| ← server | `agent_ack` | `{id, target?, latency_ms?}` | id 对应客户端发送的 command id |
| ← server | `agent_reject` | `{id, target?, reason}` | |
| → server | `agent_command` | `{id, target, priority, text}` | priority = `normal`/`high`/`urgent` |

**多 actor 处理（已决议）**：
- 前端 `state_update.uav` / `ugv` 表示「**当前焦点 actor**」，由 scenario 在启动时绑定到具体 actor id；
- 切换焦点（例如从 UAV-02 切到 UAV-01）通过更新绑定即可，前端契约不变；
- 全量 fleet（所有 UAV/UGV/红绿灯）只推给 Agent，不推给前端。

### 7.2 与 Urban Agent（CarlaBridge 定义，本期由 mock 实现）

Agent 是**单一进程**，以 Socket.IO 客户端连入 `/agent` namespace。CarlaBridge 与 Agent 共享一个 socket，事件双向：

| 方向 | 事件 | 载荷 | 说明 |
|---|---|---|---|
| → agent | `state_snapshot` | 完整世界快照（traffic_lights[] + vehicles[] + uavs[] + sim_time） | 默认 10 Hz，不裁剪；推送频率可配 |
| → agent | `event_log` | `{severity, source, message}` | 场景阶段、actor 生命周期、前端建议透传 |
| → agent | `agent_ack` | `{id, target?, latency_ms?}` | Agent 命令落地成功（与前端契约同名同结构） |
| → agent | `agent_reject` | `{id, target?, reason}` | Agent 命令被拒绝（与前端契约同名同结构） |
| → agent | `suggestion` | `{id, target, priority, text, source:"FRONTEND"}` | 来自前端的「建议入口」透传；Agent 可选择性采纳 |
| ← agent | `agent_command` | `{id, target, priority, text, payload?}` | target = actor id；text = 内部指令（见 F6.3） |
| ← agent | `event_log` | `{severity, source:"AGENT", message}` | Agent 决策汇报（如「检测到火灾」） |
| ← agent | `hello` | `{agent_id, version}` | 连接握手；可选 |

**载荷示例 — `state_snapshot`**：
```json
{
  "sim_time": 123.45,
  "traffic_lights": [{"id":"TL-12","pose":[x,y,z],"phase":"green","remaining_s":7.2}, ...],
  "vehicles":      [{"id":"UGV-01","role":"dispatchable","pose":[...],"speed":3.4,"heading":92}, ...],
  "uavs":          [{"id":"UAV-01","role":"follow","pose":[...],"altitude":85,"battery":78}, ...]
}
```

**载荷示例 — `agent_command`**（与前端契约同构）：
```json
{"id":"cmd-9f1","target":"UGV-01","priority":"high","text":"UGV_DISPATCH","payload":{"lat":31.23,"lng":121.47}}
```

### 7.3 与 CARLA
- 通过官方 Python API（`carla.Client`）连接，默认 `localhost:2000`。
- synchronous mode 由 CarlaBridge 独占。
- 不修改 CARLA 源码。

---

## 8. 数据模型（与前端 `src/types/index.ts` 对齐）

**对前端（投影后，单焦点）**：
```ts
UavTelemetry  = { id, altitude, speed, heading, battery, gps:{lat,lng}, link:{latency,quality} }
UgvTelemetry  = { id, speed, heading, road, obstacle:'safe'|'warn'|'block', battery, link }
CityMetrics   = { vehicles, pedestrians, intersections:'normal'|'congested', aqi, alerts }
SystemMetrics = { cpu, gpu, mem, net, fps }
CommandRecord = { id, timestamp, direction, target, text, latencyMs? }
```

**对 Agent（全量，不裁剪）**：
```ts
WorldSnapshot = {
  sim_time:       number,
  traffic_lights: { id, pose, phase: 'red'|'yellow'|'green', remaining_s }[],   // 真实 CARLA actor
  vehicles:       { id, role: 'dispatchable'|'civilian', pose, speed, heading, battery? }[], // 真实 CARLA actor
  uavs:           { id, role: 'patrol'|'follow'|'standby', pose, altitude, heading, battery }[], // 虚拟实体，Bridge 维护
}
```

**Actor / Entity ID 命名约定**：
- 字符串形式，Bridge 维护稳定 id 与 CARLA actor.id 的映射；destroy + respawn 时 Bridge 仍可保留同名 id
- UAV：`UAV-01` / `UAV-02` …（虚拟实体，Bridge 注册表分配）
- UGV：`UGV-01` / `UGV-02` …（真实 CARLA vehicle actor）
- 红绿灯：`TL-{carla.actor.id}`（取自 CARLA 自有整数 id，加前缀防混淆）
- Civilian vehicles：`VEH-{carla.actor.id}`

CarlaBridge 内部维护：
- `WorldSnapshot`：上述全量快照，作为唯一真相源
- `Fleet`：可控实体注册表（含真实 UGV + 虚拟 UAV）
- `FocusBinding`：前端三槽位（aerial / ground / city）当前绑定哪个 entity id
- `WorldClock`：CARLA sim time + wall time 映射
- `ScenarioState`：当前场景阶段标记

---

## 9. 验收标准

### 9.1 单元 / 接口级
- AC-1 Socket.IO 服务启动后，前端无需修改即可连上、显示 LIVE 徽标。
- AC-2 三路 WebRTC 流均能在 5 秒内建立、画面可见、无明显花屏。
- AC-3 MJPEG 兜底端点对所有摄像头 ID 可访问。
- AC-4 注入一个本地 mock Socket.IO 客户端模拟 Agent，发送 `agent_command` 后前端能收到 `agent_ack`。

### 9.2 场景级（S1 端到端）
- AC-5 启动 S1 后 ≤ 10 秒，前端三路画面 + 状态栏全部进入 LIVE。
- AC-6 整个 S1 流程（11 步）在无人干预下自动跑完。
- AC-7 关键事件全部产生对应 `event_log`：起飞、识别火源、UAV 返航、UGV 出发、UGV 到位、灭火确认、UGV 返航。
- AC-8 流程结束后 CARLA world 中无残留场景 actor。

### 9.3 性能级
- AC-9 NF1–NF7 全部达标。
- AC-10 手动 throttle 前端连接（断开 5 秒再重连）不导致 CARLA tick 暂停。

### 9.4 演示级
- AC-11 操作员通过单条命令启动 S1，无需手工预热 CARLA / Agent / 前端。
- AC-12 演示中网络断开 5 秒后前端自动回到 mock（前端能力，不破坏即可）。

---

## 10. 边界与失败模式

| 场景 | CarlaBridge 应当 |
|---|---|
| CARLA 进程未启动 / 连接失败 | 启动时报错退出，给出明确提示，不进入半启动状态 |
| Agent 未连接 | scenario 仍可启动；mock Agent 默认嵌在 scenario 内运行，外部 Agent 缺席不影响演示 |
| 前端 0 个客户端 | 仿真照常运行，状态广播照常发出（无订阅者时丢弃） |
| 前端 N 个客户端 | 全部收到广播；视频按 WebRTC 各自建链 |
| sensor 帧率高于编码速率 | 丢弃旧帧、记录 `system_metrics.fps` 下降 |
| 状态广播下游慢 | 不积压（单值原子快照），跳过本周期，记录 `event_log: severity=warn, source=BRIDGE` |
| scenario 抛异常 | 调用 `teardown()`，恢复 CARLA 模式，前端收到 `event_log: danger` |
| Agent 发非法指令 | 回 `agent_reject` 并写 `event_log: warn`，不影响其他流程 |
| 摄像头 actor 被销毁 | 该路视频流终止，其他路不受影响 |
| 关闭 CarlaBridge | 销毁 spawn actor → 恢复 async mode → 关闭 Socket.IO → 退出 |

---

## 11. 不在本期范围（明确不做）

- ❌ 前端 UI 改造
- ❌ Urban Agent 内部决策算法 / 真实图像识别（用按剧本 mock）
- ❌ 数据持久化与回放
- ❌ 用户认证、HTTPS、跨机部署
- ❌ 运行时动态增删摄像头
- ❌ 多场景并行
- ❌ Scenario DSL / YAML 配置
- ❌ UE4 Pixel Streaming 真接入

---

## 12. 决议记录（v0.1 评审已敲定，design 阶段直接采纳）

| # | 议题 | 决议 |
|---|---|---|
| D1 | 多 actor vs 前端单槽位 | **灵活绑定推送**：数据流通道接口固定（aerial/ground/city + 单焦点 uav/ugv），scenario 启动时绑定具体 actor id；切换焦点 = 改绑定，前端契约不动；全量 fleet 只推 Agent |
| D2 | mock Agent 剧本形式 | **写死**在 scenario 脚本内，按 sim_time 触发，不引入独立配置文件 |
| D3 | 防爆罐 / 机械臂动作 | **本期不实现**；UGV 到位后仅推 `event_log: arrived`，后续延迟若干秒推 `event_log: fire=extinguished` |
| D4 | 火灾标记 actor | **不准备美术素材**；用坐标标记 + event_log 表达，鸟瞰画面不强求看到火焰特效 |
| D5 | WebRTC 视频编码 | **怎么简单怎么来**，优先用 aiortc 默认编码（VP8 软编），不引入 GPU 编码依赖 |
| D6 | Socket.IO server 选型 | **怎么简单怎么来**，倾向 python-socketio + aiohttp/ASGI 单进程，与 aiortc 同事件循环 |
| D7 | 鸟瞰摄像头实现 | **简单优先**，用 CARLA spectator 视角或固定高空相机，后期再调 |
| D8 | 状态 / 视频录制回放 | **不需要** |
| D9 | UGV 自动驾驶实现（M6 真机验收后追加） | **不用 CARLA `BasicAgent`**，改用工程内的 `SimpleWaypointFollower`。原因：BasicAgent 在 sync mode + 多路 camera 真机环境下，`bounding_box` 等属性 RPC 会出现 30s 级 timeout（孤立测试无问题）；推测是 camera listener 在 CARLA 内部线程持锁与 BasicAgent 的高频 RPC 累积冲突。Follower 走 GRP 一次性建路径 + 每 tick 仅 `get_transform/get_velocity/apply_control` 三个 RPC，已在真机跑通 80m 直线 (~13 sim 秒到达)。代价：无避障 / 不识红绿灯 / 不考虑车道偏移——本期演示可接受。 |

---

## 13. 术语

| 术语 | 含义 |
|---|---|
| **Tick** | CARLA synchronous mode 下的一次仿真步进，默认 33.3 ms（30 Hz） |
| **Sensor callback** | CARLA 在每 tick 把传感器数据回调给客户端的函数，运行在 CARLA 内部线程 |
| **Scenario** | 一个完整业务流程（如 S1 火灾应急），由 Python 类描述 |
| **Bridge** | CarlaBridge 简称 |
| **Fleet** | 可被 Agent 调度的实体总清单：真实 UGV CARLA actor + 虚拟 UAV 数据实体；红绿灯不在 Fleet（属 world 原生），由 SnapshotBuilder 直接遍历 |
| **虚拟实体 / Virtual Entity** | Bridge 内部数据对象（如 UAV），无 CARLA actor 对应，pose 由 scenario 逻辑驱动 |
| **Agent / Urban Agent** | 外部单一调度系统，代替人工统一调度 UAV/UGV/交通；本期用按剧本 mock 替代 |
| **Frontend** | `D:\urban_frontend`，本期对接的唯一前端实例 |
