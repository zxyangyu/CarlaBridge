# CarlaBridge 开发任务（tasks.md）

> 文档目的：把 `design.md` 拆成可执行、可验收、可排期的任务清单。
> 上游：`spec.md` v0.1 + `design.md` v0.1。
> 状态：所有任务初始为 `todo`，按里程碑顺序推进；同一 M 内的任务允许并行处理（依赖已标注）。

---

## 0. 环境与约定

### 0.1 运行环境（已就绪，无需新建）
| 项 | 值 |
|---|---|
| OS | Windows 10 单机 |
| Python | 3.12 |
| Conda 环境路径 | `D:/carla/env` |
| CARLA | 0.9.16 |
| 默认 Map | `Town10HD_Opt` |
| 工程目录 | `D:\CarlaBridge` |
| 前端项目 | `D:\urban_frontend`（只读对齐） |

> 所有 PowerShell 命令默认假设已 `conda activate D:/carla/env`，启动脚本会在 `run.ps1` 里固化。

### 0.2 任务 ID 约定
- 格式：`T-M{milestone}-{nn}`，例：`T-M3-04`
- `S-{n}`：前置 spike（风险验证，先做）
- `X-{n}`：横向任务（贯穿所有 M）

### 0.3 任务状态字段
`todo` → `wip` → `blocked` / `done`。本文件不做状态跟踪（实际跟踪可放在 issue / kanban），但每条任务都给出 **DoD（完成定义）**。

### 0.4 估时口径
- **S**：≤ 0.5 day
- **M**：0.5 ~ 1.5 day
- **L**：1.5 ~ 3 day
- **XL**：> 3 day（应进一步拆分）

---

## 1. 里程碑总览

| ID | 名称 | 主要交付 | 关键验收 | 估时 |
|---|---|---|---|---|
| S | 前置 spike | aiortc / carla / socketio 三方在 Py3.12 + Win10 跑通 | demo 视频出图 | M |
| M0 | 工程骨架 | 目录结构 + 配置 + 空服务 + /healthz | 服务起得来 | M |
| M1 | CARLA 连接 + tick | 30 Hz 主循环，无场景干跑 | tick_fps≈30、Ctrl+C 干净退出 | M |
| M2 | Snapshot + 状态广播 | 前端 LIVE，state_update 周期 100ms | spec AC-1 | M |
| M3 | 单路 WebRTC | city 通道画面可见 | 5s 内画面就绪 | L |
| M4 | 多路相机 + 绑定 | aerial / ground / city 三路稳定 | spec AC-2 | M |
| M5 | Scenario + S1 spawn | actors 出现，无事件 | 启动场景三路画面绑定正确 | M |
| M6 | Mock Agent + S1 端到端 | S1 11 步全自动跑完 | spec AC-5/6/7 | L |
| M7 | MJPEG 兜底 + 完整健康检查 | 兜底路径可用，event_log 回放 | 离线/降级体验 OK | M |
| M8 | 验收 NF 项 + 文档 | run.ps1、README、性能数据 | spec NF1-NF8 全部达标 | M |

---

## 2. 前置 Spike

> 用户已验证选型（aiortc / carla 0.9.16 / python-socketio + aiohttp 同 loop 共栈）。本节保留作为环境复核 checklist，不作为前置阻塞任务。

- ✅ aiortc on Win10 + Py3.12
- ✅ CARLA 0.9.16 + Py3.12 + Town10HD_Opt
- ✅ python-socketio + aiohttp + aiortc 同 loop

如后续开发中遇到环境问题，回到对应 spike 记录排查。

---

## 3. M0 — 工程骨架

### T-M0-01 项目目录与 pyproject
- 创建 `carlabridge/` 包，目录结构对齐 design §4。
- 初始化 `pyproject.toml`，依赖：`aiohttp`、`python-socketio`、`aiortc`、`av`、`numpy`、`pydantic-settings`、`psutil`、`pytest`、`pytest-asyncio`、`tomli-w`、`carla`（来自 CARLA 安装）。
- 把 CARLA `PythonAPI/carla/agents` 目录拷贝或软链到工程内（BasicAgent 需要），或加入 PYTHONPATH。
- 添加 `.gitignore`（pyc / __pycache__ / logs/ / *.log / .env）。
- **DoD**：`pip install -e .` 在 `D:/carla/env` 内成功；`from agents.navigation.basic_agent import BasicAgent` 可导入。
- **依赖**：无
- **估时**：S

### T-M0-02 config.py + default.toml
- pydantic-settings Settings 类，支持 TOML + env 覆盖。
- 字段对齐 design §12（carla / server / broadcast / video / agent / scenario / logging）。
- **DoD**：单元测试 `tests/test_config.py` 验证 TOML 解析与 env 覆盖。
- **依赖**：T-M0-01
- **估时**：S

### T-M0-03 main.py 启动骨架
- argparse: `--config`、`--scenario`、`--log-level`
- SIGINT handler 设置 shutdown_event
- 空 aiohttp app + python-socketio AsyncServer（两个 namespace 注册空 handler）
- **DoD**：`python -m carlabridge.main` 启动后 listen `0.0.0.0:5000`，Ctrl+C 干净退出。
- **依赖**：T-M0-02
- **估时**：S

### T-M0-04 /healthz 占位
- 简单返回 `{"status": "alive"}`。
- **DoD**：浏览器访问 `http://localhost:5000/healthz` 返回 200。
- **依赖**：T-M0-03
- **估时**：S

### T-M0-05 obs 占位
- `obs/event_log.py`：环形缓冲 + add(event) + recent(n)（暂不广播）
- `obs/metrics.py`：dict 容器 + set/get（暂不采样）
- **DoD**：单元测试覆盖 add / recent / 容量截断。
- **依赖**：T-M0-01
- **估时**：S

### T-M0-06 run.ps1 启动脚本
- 自动 activate conda env、加 PYTHONPATH、运行 main。
- **DoD**：`./run.ps1` 等价于 T-M0-03。
- **依赖**：T-M0-03
- **估时**：S

---

## 4. M1 — CARLA 连接 + tick

### T-M1-01 core/world.py
- `World.connect(host, port, timeout)`，`save_original_settings()` / `ensure_map(name)` / `switch_to_sync(delta)` / `restore_original_settings()` / `disconnect()`。
- `ensure_map`：当前 map 不匹配 cfg.carla.map 时 `world.load_world(name)` 并等待加载完成。
- 异常处理：连接失败抛 `BridgeFatal`。
- **DoD**：用真 CARLA 实例 connect → ensure_map("Town10HD_Opt") → switch → restore；二次 connect 验证 settings 已恢复，map 仍是 Town10HD。
- **依赖**：T-M0-01
- **估时**：M

### T-M1-02 core/clock.py
- `SimClock`：维护 `sim_time`（accumulated delta）+ `wall_time`（time.monotonic）。
- **DoD**：单元测试 tick N 次后 sim_time 正确累加。
- **依赖**：T-M0-01
- **估时**：S

### T-M1-03 core/fleet.py
- `Fleet`：注册/反注册可控实体，同时支持两类成员：
  - **CarlaActorMember**：包装真实 CARLA actor（UGV / civilian 等）
  - **VirtualMember**：纯数据实体（虚拟 UAV），含 pose / battery / role / target / step(dt) 方法
- 按角色（patrol/follow/standby/dispatchable/civilian）查询。
- **DoD**：单元测试覆盖两类成员的注册、查询、按角色过滤；virtual member 的 step(dt) 能推进 pose。
- **依赖**：T-M0-01
- **估时**：M

### T-M1-04 core/tick_loop.py
- 独立线程 `TickLoop`：循环结构 = drain_commands → scenario.on_tick_pre → world.tick → build_snapshot → scenario.on_tick_post → sleep_until。
- 频率控制：`sleep_until(t0 + 1/hz)`，记录实际 fps。
- shutdown_event 检测。
- 空 scenario 接口（M1 阶段直接传一个 NoopScenario）。
- **DoD**：跑 30 秒，实测 fps 在 28–32 之间。
- **依赖**：T-M1-01、T-M1-02
- **估时**：M

### T-M1-05 metrics tick_fps 采样
- 1 Hz 计算最近 1s 的实际 tick 数。
- 写入 `obs.metrics`。
- **DoD**：单元测试模拟 30 tick → fps=30。
- **依赖**：T-M1-04、T-M0-05
- **估时**：S

### T-M1-06 main.py 集成 M1
- 装配顺序：connect → switch_to_sync → start tick thread → run aiohttp → on shutdown join thread → restore mode → disconnect。
- **DoD**：CARLA 启动状态下，`./run.ps1` 启动 bridge，`/healthz` 显示 `tick_fps` 字段被 1 Hz 更新；优雅关停后 CARLA world 回到 async mode（用另一脚本验证）。
- **依赖**：T-M1-04、T-M0-03
- **估时**：S
- **集成验收已知约束**：实测 tick_fps 取决于 CARLA 渲染吞吐。当前演示机 quality=Low/800x600 下 `world.tick()` ~110ms/帧 → tick_fps ≈ 9 Hz。Bridge pacing 逻辑正确（FakeWorld 单元测试 30 Hz 稳定），CARLA 渲染是瓶颈。NF1 最终验收需在能跑满 30 Hz 的硬件 + 配置下进行；演示机上接受 ~9 Hz 配 30 Hz 目标的 warning 日志。
- **集成验收修订收益**：原代码在 `switch_to_sync` 之后、HTTP/tick 启动之前若抛非 `BridgeFatal` 异常（如端口被占的 `OSError 10048`），CARLA 会被卡在 sync mode 里没人 restore。已重构 `_run()` 把整个 setup + run 包进一个外层 try/finally，强制 restore。同时新增 `POST /admin/shutdown` 用于运维/测试场景的程序化优雅关停（Windows 上 `Stop-Process` 等同 TerminateProcess，没办法走 finally；终端里手动 Ctrl+C 仍走 SIGINT handler 正常路径）。

---

## 5. M2 — Snapshot + 状态广播

### T-M2-01 core/snapshot.py
- `WorldSnapshot` dataclass：sim_time、traffic_lights[]、vehicles[]、uavs[]。
- 子类型：`TrafficLightState`、`VehicleState`、`UavState`，对齐 spec §8。
- **DoD**：dataclass 字段完整，可 JSON 序列化（`asdict`）。
- **依赖**：T-M0-01
- **估时**：S

### T-M2-02 SnapshotBuilder.build()
- 输入：world + fleet；输出：WorldSnapshot。
- 实现三个分区：
  - **traffic_lights**：`world.get_actors().filter('traffic.traffic_light')`，读 `state` + `get_red_time/get_green_time/get_yellow_time`。
  - **vehicles**：从 Fleet 的 CarlaActorMember 取 dispatchable（UGV）+ 可选过滤后的 civilian。
  - **uavs**：从 Fleet 的 VirtualMember 直接取。
- **DoD**：集成测试 — 在 Town10HD 启动场景后 spawn 1 UGV + 注册 3 虚拟 UAV，build 输出含 traffic_lights/vehicles/uavs 三部分非空。
- **依赖**：T-M2-01、T-M1-03
- **估时**：M

### T-M2-03 Atomic[Snapshot] 容器
- 简单封装：`AtomicRef[T]` with `set(v)` / `get() -> T`。CPython 单变量赋值原子，无需锁。
- **DoD**：单元测试多线程 set/get 不抛错。
- **依赖**：T-M0-01
- **估时**：S

### T-M2-04 bus/projector.py
- `for_frontend(snap, focus_binding)` → `{uav, ugv, city}`：按 focus_binding 选出 UAV/UGV，聚合 city 统计。
- `for_agent(snap)` → 全量。
- **DoD**：单元测试两种投影输出字段对齐契约。
- **依赖**：T-M2-01
- **估时**：M

### T-M2-05 bus/server.py（socketio + aiohttp 装配）
- 创建 AsyncServer，挂到 aiohttp app（路径 `/socket.io/`）。
- 注册 `/` 和 `/agent` 两个 namespace 的 connect/disconnect handler。
- **DoD**：前端 vite dev `npm run dev` 连入 `localhost:5000` 后控制台显示 "connect"，bridge 日志显示 sid。
- **依赖**：T-M0-03
- **估时**：M

### T-M2-06 bus/frontend_ns.py
- connect：发送一次全量初始快照（投影后）。
- disconnect：清理 sid。
- `agent_command` 接收 → event_log 记录 → 透传给 `/agent`（M6 真实实现时补，本里程碑可仅记录）。
- **DoD**：前端连入立即拿到 state_update。
- **依赖**：T-M2-05、T-M2-04
- **估时**：S

### T-M2-07 bus/agent_ns.py（read 侧）
- connect / disconnect 同上。
- `agent_command` 接收 → 入 command_queue（M6 实现）；本里程碑 stub。
- **DoD**：mock socketio-client 连入 `/agent` 收到 state_snapshot。
- **依赖**：T-M2-05
- **估时**：S

### T-M2-08 bus/broadcaster.py
- 10 Hz async task。
- 每周期读 AtomicRef、投影、emit 三类事件：`state_update`（/）、`state_snapshot`（/agent）、`system_metrics`（/，1 Hz）。
- **DoD**：日志看到 broadcast 周期稳定 100ms±10ms，前端 store 内 vehicles 数字跳动。
- **依赖**：T-M2-03、T-M2-04、T-M2-05
- **估时**：M

### T-M2-09 main.py 集成 M2
- 装配 broadcaster task + Atomic[Snapshot] 全局实例 + tick_loop 写入。
- **DoD**：spec **AC-1** 通过：前端连入立即 LIVE。
- **依赖**：T-M2-08、T-M1-06
- **估时**：S

---

## 6. M3 — 单路相机 + WebRTC（city 优先）

### T-M3-01 sensors/frame_queue.py
- 单元素 latest-wins 队列：`set_latest(frame)` 永不阻塞、`async get()` 等下一帧。
- 内置 drop counter。
- **DoD**：单元测试：连续 set 100 次 + 1 次 get → 拿到最后一帧 + drop=99。
- **依赖**：T-M0-01
- **估时**：S

### T-M3-02 sensors/camera.py
- `CameraSpec` dataclass（id / attach_to / pose / fov / resolution / fps）。
- `spawn_camera(world, spec)` → 注册 listener，listener 把 raw frame 投递到对应 FrameQueue（通过 `loop.call_soon_threadsafe`）。
- `detach_all()`。
- **DoD**：spawn city 高空相机，listener 调用计数 ≈ 配置 fps。
- **依赖**：T-M3-01、T-M1-01
- **估时**：M

### T-M3-03 streaming/webrtc.py — CameraTrack
- aiortc `VideoStreamTrack` 子类，`recv()` 从 FrameQueue 取帧，numpy → `av.VideoFrame`（BGR/RGB 转换）。
- 设置 pts/time_base。
- **DoD**：单元测试 mock FrameQueue 喂入 numpy，recv() 返回 av.VideoFrame。
- **依赖**：T-M3-01、S-01
- **估时**：M

### T-M3-04 streaming/webrtc.py — signaling 路由
- `POST /webrtc/<camera_id>`：解析 SDP offer → 创建 RTCPeerConnection → addTrack(CameraTrack) → setRemoteDescription → createAnswer → 返回 JSON。
- 维护 sessions dict，断开时清理。
- **DoD**：前端 city 面板点 connect → WebRTC 5s 内画面就绪。
- **依赖**：T-M3-03、T-M2-05
- **估时**：M

### T-M3-05 main.py 集成 M3（硬编码 city）
- 启动时 spawn 一个 city 相机（spectator 模式）。
- **DoD**：前端三个面板里 city 出图，其他两个仍 placeholder。
- **依赖**：T-M3-04、T-M3-02
- **估时**：S

---

## 7. M4 — 多路相机 + 绑定模型

### T-M4-01 CameraBindings & BindingTable
- `CameraBinding`（attach_to / world_pose / 等），`BindingTable` 持有 `{channel_id: binding}`。
- 提供 `rebind(channel_id, new_actor_id)` 接口。
- **DoD**：单元测试：rebind 后查询返回新绑定。
- **依赖**：T-M3-02
- **估时**：S

### T-M4-02 三种 camera mode 实现
- `attached_to_actor`：spawn 时 attach 到 CARLA actor（用于 UGV ground 相机）
- `follows_virtual`：spawn 不 attach，每 tick post 由 scenario hook 调用 `sensor.set_transform(virtual_pose + offset)`（用于 UAV aerial 相机）
- `world_pose`：spawn 不 attach，一次性 set_transform（用于 city）
- **DoD**：三种 mode 各 spawn 一路，观察跟随行为正确；ground 跟车、aerial 跟虚拟 UAV 移动、city 静止。
- **依赖**：T-M4-01、T-M1-03
- **估时**：M

### T-M4-03 hot rebind（FrameQueue 切源）
- rebind 时：销毁旧 sensor、spawn 新 sensor（保留同一个 FrameQueue），WebRTC track 不重连。
- **DoD**：运行中调用 rebind("aerial", "UAV-02")，前端 aerial 画面无重连切换源。
- **依赖**：T-M4-02
- **估时**：M

### T-M4-04 city 高空相机方案
- D7：用 spectator 视角的 transform 或独立高空相机 actor。
- 实现选简单方案，pose 写在配置或 scenario 里。
- **DoD**：city 通道画面稳定，俯瞰角度合理。
- **依赖**：T-M4-01
- **估时**：S

### T-M4-05 三路同跑性能 sanity
- 同时跑 aerial/ground/city，VP8 720p@25fps，跑 5 分钟。
- **DoD**：spec **AC-2**（三路 5s 建链）+ CPU < 80% + 无 frame drop > 5%。
- **依赖**：T-M4-02、T-M4-04
- **估时**：S

---

## 8. M5 — Scenario 引擎 + S1 spawn

### T-M5-01 scenarios/base.py
- `Scenario` ABC：`setup() / on_tick_pre() / on_tick_post() / on_command() / teardown() / setup_bindings()`。
- 注册装饰器 `@scenario("s1_fire")` 或简单 registry dict。
- **DoD**：注册一个 NoopScenario，runner 能加载它。
- **依赖**：T-M0-01
- **估时**：S

### T-M5-02 scenarios/runner.py
- `ScenarioRunner`：start(name) → setup → 接管 tick_loop hooks；stop() → teardown。
- 保存 sim_time，提供给 mock agent 协程查询。
- **DoD**：runner 单元测试：start/stop NoopScenario 不抛错。
- **依赖**：T-M5-01、T-M1-04
- **估时**：M

### T-M5-03 scenarios/s1_fire.py — setup
- **注册 3 架虚拟 UAV**（VirtualMember，patrol 角色）+ spawn 1 UGV CARLA actor（dispatchable 角色）。
- spawn 火灾标记（D4：streetbarrier 或类似 prop，仅作坐标锚点）。
- 设定坐标：火源、UAV 巡逻路径、UGV 起点 —— **基于 Town10HD_Opt 实际坐标**写死在 scenario 内。
- setup_bindings：aerial=`follows_virtual` UAV-01, ground=`attached_to_actor` UGV-01, city=`world_pose` 高空俯视。
- **DoD**：启动场景后前端三路画面绑定正确，UGV 在 CARLA world 中可见，虚拟 UAV 通过 state_snapshot 可被 Agent 读取。
- **依赖**：T-M5-02、T-M4-02、T-M4-04
- **估时**：M

### T-M5-04 teardown
- 销毁所有 spawn 的 actor + camera。
- **DoD**：重复启停 5 次无残留 actor（用 `world.get_actors()` 验证）。
- **依赖**：T-M5-03
- **估时**：S

### T-M5-05 启动入口
- `--scenario s1_fire` 加载该场景。
- **DoD**：`./run.ps1 --scenario s1_fire` 一键启动到「actors 就位」状态。
- **依赖**：T-M5-03
- **估时**：S

---

## 9. M6 — 命令通路 + Mock Agent + S1 端到端

### T-M6-01 commands/enum.py
- `CommandKind` 枚举：`UAV_RTL / UAV_HOLD / UGV_DISPATCH / UGV_RTL / MARK_EVENT / ATTACH_ACTOR`。
- `ParsedCommand` dataclass：kind / target / payload / id。
- **DoD**：枚举与 spec F6.3 一一对应。
- **依赖**：T-M0-01
- **估时**：S

### T-M6-02 commands/dispatcher.py
- `parse(raw_dict) -> ParsedCommand`，未知 kind → 抛 `RejectCommand`。
- **DoD**：单元测试 6 种合法 + 1 种非法。
- **依赖**：T-M6-01
- **估时**：S

### T-M6-03 跨域 command_queue
- `queue.Queue(maxsize=64)`，async 侧 `put_nowait`、tick 侧 drain。
- 满时同步回 `agent_reject(reason="overloaded")`。
- **DoD**：单元测试满队列拒绝逻辑。
- **依赖**：T-M6-02
- **估时**：S

### T-M6-04 agent_ns 接入 dispatcher
- 收到 `agent_command` → parse → put 到 command_queue → 若解析失败立即 reject。
- **DoD**：用 mock socketio-client 发 6 种命令，前 5 种入队、最后 1 种被 reject。
- **依赖**：T-M6-03、T-M2-07
- **估时**：S

### T-M6-05 tick_loop drain + scenario.on_command
- 每 tick 开头 drain command_queue → `scenario.on_command(cmd)` → ack/reject。
- ack/reject 用 `loop.call_soon_threadsafe(sio.emit, ...)` 发回 / 和 /agent。
- **DoD**：发一个 UAV_RTL，前端与 agent 都收到 ack。
- **依赖**：T-M6-04、T-M5-02
- **估时**：M

### T-M6-06 agent/link.py
- `AgentLink` 抽象：`async emit_command(cmd) / emit_event_log(...) / on_state_snapshot(...)`。
- **DoD**：interface 编译通过，文档清晰。
- **依赖**：T-M0-01
- **估时**：S

### T-M6-07 agent/mock_agent.py
- `MockAgentLink`：emit_command 直接走 dispatcher（不绕 Socket.IO）。
- **DoD**：单元测试触发一条命令，dispatcher 收到。
- **依赖**：T-M6-06、T-M6-02
- **估时**：S

### T-M6-08 agent/socketio_agent.py
- `SocketIOAgentLink`：把 `/agent` namespace 的远程客户端事件桥接到 `AgentLink`。
- 本期可保持 stub（无远程 Agent 接入），但接口齐全便于未来接入。
- **DoD**：interface 编译通过、保留 TODO 注释。
- **依赖**：T-M6-06
- **估时**：S

### T-M6-09 scenarios/s1_fire.py — SCRIPT + mock_agent_loop
- `SCRIPT` 写死 11 步关键事件（sim_time → 事件）。
- `mock_agent_loop(link)` 协程：按 sim_time 触发 emit_command / emit_event_log。
- **DoD**：场景跑起来后日志显示按预期触发事件。
- **依赖**：T-M6-07、T-M5-03
- **估时**：M

### T-M6-10 scenarios/s1_fire.py — on_command 实现
- `UAV_RTL` / `UAV_HOLD`：更新虚拟 UAV 的 target（RTL → 起点坐标 + 降高度；HOLD → 当前坐标 + 保持高度）。虚拟 UAV 每 tick 在 VirtualMember.step(dt) 中向 target 做插值移动。
- `UGV_DISPATCH`：用 **BasicAgent**：
  - `self.ugv_agent = BasicAgent(ugv_actor, target_speed=30)`
  - `self.ugv_agent.set_destination(carla.Location(*payload))`
  - tick post 调 `ctrl = ugv_agent.run_step(); ugv_actor.apply_control(ctrl)`
  - 30s 无进度（距 target 距离不下降）→ fail + agent_reject
- `UGV_RTL`：同 DISPATCH，destination=起点；`done()` 时推 event_log: returned。
- `MARK_EVENT`：写 event_log。
- `ATTACH_ACTOR`：D3 决议本期不实现，return silently + 记 event_log。
- **DoD**：手动发命令测试：UGV 能从起点开到火源坐标并到达（`done()=True`）；虚拟 UAV 能 RTL/HOLD。
- **依赖**：T-M6-05、T-M5-03
- **估时**：L
- **风险**：BasicAgent 在 Town10HD 某些 spawn 点可能找不到路径 → 备选实现简易 waypoint follower（参考 GlobalRoutePlanner）。

### T-M6-11 端到端 S1 集成
- 串起来跑：启动 → mock 触发火警 → UAV_RTL × 2 → UGV_DISPATCH → 到位 → fire=extinguished → UGV_RTL → 完成。
- **DoD**：spec **AC-5 / AC-6 / AC-7** 通过。
- **依赖**：T-M6-09、T-M6-10
- **估时**：M

### T-M6-12 前端「建议」透传
- 前端发 `agent_command` → frontend_ns handler 写 event_log + 调 `AgentLink.on_suggestion(payload)`。
- `MockAgentLink.on_suggestion`：投递到 mock_agent_loop 的内部 suggestion queue；本期 mock 默认策略 = 「忽略并 reject」（reason="mock mode, suggestions not honored"），但代码留 hook 便于演示时手动改成「采纳」。
- `SocketIOAgentLink.on_suggestion`：emit `suggestion` 事件到远程 Agent，由 Agent 决定 ack/reject。
- **DoD**：前端 CommandPanel 发命令后 500ms 内看到 agent_reject 日志；NF4 可验。
- **依赖**：T-M6-04、T-M6-06
- **估时**：S

---

## 10. M7 — MJPEG 兜底 + 完整健康检查 + event_log 持久缓冲

### T-M7-01 streaming/jpeg_tap.py
- 按需 JPEG 编码：只有 MJPEG 客户端连入时才启用。
- 用 PyAV 或 PIL。
- **DoD**：单元测试 numpy → JPEG bytes。
- **依赖**：T-M3-01
- **估时**：S

### T-M7-02 streaming/mjpeg.py
- `GET /video_feed?camera=<id>`：multipart/x-mixed-replace 流。
- 客户端断开自动停止 tap。
- **DoD**：浏览器直接打开 URL 看到画面。
- **依赖**：T-M7-01
- **估时**：M

### T-M7-03 event_log 广播 + 客户端首连回放
- 新客户端连入 `/` 时回放最近 100 条。
- 任何 event_log.add() 触发 broadcast 给所有 `/` 与 `/agent`。
- **DoD**：前端刷新页面后能看到刚才的历史事件。
- **依赖**：T-M0-05、T-M2-05
- **估时**：S

### T-M7-04 /healthz 完整字段
- 按 design §15.4 输出 carla / tick_fps / scenario / clients / cameras。
- **DoD**：JSON 字段全、值正确。
- **依赖**：T-M0-04、T-M2-08、T-M3-04
- **估时**：S

---

## 11. M8 — 验收 NF 项 + 文档

### T-M8-01 30 分钟稳定性测试
- 跑 S1 启动后挂机 30min（场景会 idle，因为 SCRIPT 已经跑完，可加循环或自动重启场景）。
- 监控 tick_fps、内存。
- **DoD**：NF1（tick 抖动 < ±2 Hz）+ NF5（内存增长 < 200MB）通过。
- **依赖**：M7 全部
- **估时**：S

### T-M8-02 注入延迟测试
- 模拟前端慢响应（在 broadcaster 里加 sleep）→ 验证 tick 不卡顿。
- **DoD**：NF6 通过。
- **依赖**：M2 全部
- **估时**：S

### T-M8-03 5 次启停验证
- 连续启动 → 跑完 S1 → 关闭，5 轮。
- 检查无残留 actor、无端口占用。
- **DoD**：NF7 + AC-8 通过。
- **依赖**：M6 全部
- **估时**：S

### T-M8-04 README.md
- 含环境要求、启动步骤、配置说明、scenario 编写指引、常见故障。
- **DoD**：新人按 README 能在 30 分钟内启动 S1。
- **依赖**：所有 M
- **估时**：M

### T-M8-05 run.ps1 完善
- 自动激活 conda env、设置 PYTHONPATH、传参透传、支持 `--scenario`。
- **DoD**：`.\run.ps1 --scenario s1_fire` 一键启动。
- **依赖**：T-M0-06
- **估时**：S

---

## 12. 横向任务（贯穿）

### X-01 单元测试覆盖
- 每个核心模块至少 70% 行覆盖：projector / dispatcher / snapshot / frame_queue / scenario state machine。
- **DoD**：`pytest -q` 全绿。
- **依赖**：随模块同步推进
- **估时**：随任务累计

### X-02 集成测试（FakeWorld stub）
- `tests/fakes/fake_world.py`：实现 CARLA world 的最小子集，让 tick + snapshot 链路无 CARLA 也能跑。
- **DoD**：CI 阶段无需 CARLA 也能跑核心链路测试。
- **依赖**：T-M2-02
- **估时**：M

### X-03 结构化日志
- stdlib logging + JSON formatter，关键路径打结构化字段。
- **DoD**：日志文件可被 jq 解析。
- **依赖**：T-M0-03
- **估时**：S

### X-04 git 仓库初始化
- `git init` + 初始 commit（spec.md + design.md + tasks.md + skeleton）。
- **DoD**：git log 能看到首个 commit。
- **依赖**：T-M0-01
- **估时**：XS

### X-05 性能采样脚本
- 简单脚本读 /healthz + system_metrics，写 CSV。
- **DoD**：M8 验收时直接用此脚本生成图表数据。
- **依赖**：T-M7-04
- **估时**：S

---

## 13. 依赖关系总览（关键路径）

```
S-01 ─┐
S-02 ─┼─► T-M0-01 ─► T-M0-02 ─► T-M0-03 ─► T-M0-04
S-03 ─┘                          │
                                 ▼
                              T-M1-01 ─► T-M1-04 ─► T-M1-06 ─► T-M2-09 (AC-1 ✓)
                                                                  │
                                                                  ▼
                                          T-M2-02 ─► T-M2-04 ─► T-M2-08
                                                                  │
                              T-M3-01 ─► T-M3-02 ─► T-M3-03 ─► T-M3-04 ─► T-M3-05
                                                                  │
                                          T-M4-01 ─► T-M4-02 ─► T-M4-05 (AC-2 ✓)
                                                       │
                                                       ▼
                                          T-M5-02 ─► T-M5-03 ─► T-M5-05
                                                       │
                                                       ▼
                              T-M6-02 ─► T-M6-04 ─► T-M6-05 ─► T-M6-10 ─► T-M6-11 (AC-5/6/7 ✓)
                                                                  │
                                                                  ▼
                                                       T-M7 全部 ─► T-M8 全部 (NF ✓)
```

**关键路径上的高风险节点**：
- ⚠ **S-01**：aiortc on Py3.12，若不可行后续所有 M3+ 任务受阻 → 启动当天必做。
- ⚠ **T-M2-02 SnapshotBuilder**：需要熟悉 CARLA 0.9.16 actor API（traffic light 相位 / vehicle telemetry），建议先 spike 出最小读取脚本。
- ⚠ **T-M6-10 on_command 实现**：UGV 自动驾驶到目标点是 S1 演示的关键，CARLA 自带 autopilot 不一定按指定坐标走，可能要写 simple route planner，留出余量。

---

## 14. 不在本任务清单（推迟）

对应 spec §2.3 / §11 不在范围 + design §1 非目标：

- ❌ 增强场景 S2（管控区抓捕）
- ❌ 真实 Urban Agent 接入（保留 `SocketIOAgentLink` stub）
- ❌ UE4 Pixel Streaming
- ❌ Scenario DSL / YAML
- ❌ 运行时动态增删摄像头
- ❌ 防爆罐 / 机械臂视觉化
- ❌ 用户认证 / TLS
- ❌ 数据持久化 / 录制回放
- ❌ 多前端 / 多 Agent 并发压测

---

## 15. 验收清单交叉引用

| spec 验收 | 完成于任务 |
|---|---|
| AC-1 前端 LIVE | T-M2-09 |
| AC-2 三路 WebRTC ≤5s | T-M4-05 |
| AC-3 MJPEG 兜底 | T-M7-02 |
| AC-4 mock agent_command ack | T-M6-05 |
| AC-5 S1 启动 ≤10s | T-M6-11 |
| AC-6 S1 全流程跑完 | T-M6-11 |
| AC-7 event_log 完整 | T-M6-11 |
| AC-8 无残留 actor | T-M5-04 + T-M8-03 |
| AC-9 NF 全达标 | T-M8-01/02/03 |
| AC-10 网络抖动不影响 tick | T-M8-02 |
| AC-11 单条命令启动 | T-M8-05 |
| AC-12 前端自动回 mock | 前端自有能力，不破坏即可 |
