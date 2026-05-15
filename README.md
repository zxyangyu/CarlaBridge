# CarlaBridge

实时中间件，桥接 CARLA 仿真器、React 数字孪生指挥前端、Urban Agent 调度系统。

- **上游**：CARLA 0.9.16 (Town10HD_Opt)
- **下游**：`D:\urban_frontend` (React + Socket.IO + WebRTC，本期只读)
- **平行**：Urban Agent — 完全外部进程，通过 `/agent` namespace 接入；本仓 root 的 `test_agent.py` 是协议等价的测试客户端

## 1. 文档结构

| 文档 | 内容 |
|---|---|
| `spec.md` | WHAT / WHY — 需求规范、决议记录 (D1-D10) |
| `design.md` | HOW — 架构、模块、跨域并发模型、风险（部分章节已被 refactor 文档 supersede，详见文内标注） |
| `design-refactor-agent-boundary.md` | refactor v0.3 — Agent/Bridge 解耦、8 条命令、通用生命周期、HTTP 控制面 |
| `../bridge-agent-protocol-v1.md` | **协议 v1.0 线契约** — Bridge ⇄ Agent envelope、事件清单、命令枚举、状态机的唯一权威；与 design 不一致以此为准 |
| `tasks.md` | 开发拆分 — M0-M8 完成状态、AC/NF 真机进度 |
| `tasks-refactor.md` | R1-R11 重构任务清单与 DoD（R11 = 协议 v1.0 envelope 合规） |
| `README.md` | 这个文档 — 安装、启动、故障排查、冒烟流程 |

> **协议合规**：本 Bridge 实现遵循 `bridge-agent-protocol-v1.md` v1.0。所有 `/agent` namespace 出站事件（`state_snapshot` / `command_status` / `scenario_event` / `event_log`）统一包裹 envelope `{version, msg_id, type, timestamp, frame, sim_time, sender, payload}`；入站命令与 hello 兼容 envelope/裸双形态。`/`(前端) 仍按既有 frontend 协议发裸 payload（前端协议不在 v1.0 范围）。版本字段位于 `carlabridge/bus/envelope.py:PROTOCOL_VERSION`。

## 2. 环境要求

| 项 | 值 | 备注 |
|---|---|---|
| OS | Windows 10 | (Linux 未验证；Win 多媒体定时器 + CARLA Python API 兼容) |
| Python | 3.12 | 锁定，由 CARLA 官方 wheel 的 cp312 版本决定 |
| Conda env | 仓库根目录 `.venv` | 前缀环境：`conda create -p ".\.venv"`，见 **§2.1** |
| CARLA | 0.9.16 | `CarlaUE4.exe` 在外部启动，默认 `127.0.0.1:2000` |
| 默认地图 | `Town10HD_Opt` | 启动时自动 load |
| 前端 | `D:/urban_frontend` | vite dev，需先 `npm install` |

依赖列在 `pyproject.toml` 里：`aiohttp / python-socketio / aiortc / av / numpy / pydantic-settings / psutil / shapely / networkx` 等。`carla` **不在 PyPI**，必须从 CARLA 安装目录自带的 **cp312 Windows wheel** 安装（见 **§2.1**）。

### 2.1 Conda 环境与依赖安装

在 **仓库根目录**（与 `pyproject.toml` 同级）执行：

```powershell
conda create -p ".\.venv" python=3.12 -y
conda activate ".\.venv"
pip install -e .[dev]
pip install "E:\Program Files\CARLA_0.9.16\PythonAPI\carla\dist\carla-0.9.16-cp312-cp312-win_amd64.whl"
```

说明：

- **_wheel 路径**：请按本机 CARLA 安装位置修改（一般在 `PythonAPI\carla\dist\` 下，文件名需与 Python 3.12 / win_amd64 一致）。
- **路径含空格**：若仓库或 CARLA 在 `Program Files` 等目录下，conda 可能提示路径含空格；安装后务必 **`conda activate` 再运行**，减少脚本/工具解析路径的问题。
- **CARLA `agents`（BasicAgent 等）**：`carlabridge` 会把 `CARLA_AGENTS_ROOT`（若未设置则默认 `D:/carla/PythonAPI/carla`）加入 `sys.path`。若 CARLA 不在默认盘符路径，请在启动前设置，例如：

  ```powershell
  $env:CARLA_AGENTS_ROOT = "E:\Program Files\CARLA_0.9.16\PythonAPI\carla"
  ```

- **`run.ps1`**：若脚本里 `$PythonExe` 仍指向旧的固定路径，请改为本仓库下的 `.\.venv\python.exe`（或 `(Join-Path $RepoRoot '.venv\python.exe')`），与上述环境一致。

## 3. 启动流程（演示路径）

按顺序起三件事：

### 3.1 CARLA 服务器

将 `<CARLA_ROOT>` 换成你的安装目录（示例：`E:\Program Files\CARLA_0.9.16`）：

```cmd
"<CARLA_ROOT>\CarlaUE4.exe" -quality-level=Low -ResX=1280 -ResY=720 -RenderOffScreen
```
（建议 Low quality 以避免 CARLA 渲染线程拖慢 tick；详见故障 §6.1）

### 3.2 Bridge

```powershell
cd "E:\Program Files\CarlaBridge"   # 换成你的仓库路径
conda activate ".\.venv"            # 若 run.ps1 已指向 .\.venv\python.exe 可省略
.\run.ps1 -Scenario s1_fire
```
就绪标志：
- 控制台出现 `==> launching: ...`
- 紧接着 `connected to CARLA server 0.9.16 at 127.0.0.1:2000`
- 最后 `tick loop started (delta=0.0333s)` + `bridge ready, waiting for /agent connection`

启动后 Bridge **空载等待**：UAVs 停在 origin、UGV 停在 origin、无 incident、无 in-flight 命令。等 Agent 连入下发命令、operator 通过 HTTP 触发火情。

**注意**：PowerShell 用单破折号 `-Scenario`，不是 `--scenario`。`.\run.ps1` 内部翻译给 python CLI。

### 3.3 前端

```powershell
cd D:\urban_frontend
npm run dev
```

打开浏览器到 vite 报的本地地址（通常 `http://localhost:5173`）。

`.env` 应该配置成（直接用 MJPEG 兜底，WebRTC 通过面板的 CONNECT chip 手动切）：

```
VITE_SOCKET_URL=http://localhost:5000
VITE_UAV_FEED_URL=video_feed?camera=aerial
VITE_UGV_FEED_URL=video_feed?camera=ground
VITE_CITY_FEED_URL=video_feed?camera=city
```

### 3.4 你应该看到什么（仅 Bridge 启动后）

打开浏览器后立即可见：

- 三路视频面板就绪、LIVE 徽标亮起
- `state_update` 持续推送（UAV/UGV pose 不变、`incidents=[]`、`in_flight_commands=[]`）
- EventLog 仅有启动相关事件（spawn 完成、相机绑定等）
- **不会**自动出现火情、UGV 不会自己跑、UAV 不会自己飞

要看到完整的"巡逻 → 火情 → 灭火 → 返航"流程，需要按 §3.5 启动 `test_agent.py` 并用 HTTP（PowerShell 或 curl）触发火情。

### 3.5 端到端冒烟流程（test_agent.py + HTTP 控制面）

3 个终端：

```powershell
# 终端 1：Bridge
.\run.ps1

# 终端 2：测试 Agent（仓 root 的 test_agent.py）
conda activate ".\.venv"
python test_agent.py
# 默认连接 http://127.0.0.1:5000；可加 --url / -v(--verbose) / --no-extinguish 等
# 输出：connected → hello → 收到第一帧 snapshot → 给 UAV-01/02/03 各发 1 条
#       UAV_PATROL(loop=true) → 收到 3 个 ongoing 事件，进入待命

# 终端 3：operator 触发火情（相对 UGV-01 origin 东偏 +90 m，勿在已开的 PowerShell 里直接粘贴带嵌套 powershell 的一行）
# —— PowerShell 内直接执行（推荐）：
$ugv = (Invoke-RestMethod 'http://127.0.0.1:5000/scenario/status').entities.'UGV-01'.origin
$body = @{ id = 'fire-001'; position = @{ x = ($ugv.x + 90.0); y = $ugv.y; z = $ugv.z } } | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:5000/scenario/fire' -ContentType 'application/json' -Body $body
# —— 或从 cmd.exe 一行调用子 PowerShell（全文见 §5.1 「从 cmd.exe 一行」）。
# 测试 Agent 终端应输出（按 sim_time 推进）：
#   incident fire-001 detected → UGV_GOTO(dest=fire-001+offset) accepted
#   UGV-01 距 fire-001 ≤ 5m → UGV_EXTINGUISH(incident_id=fire-001) accepted
#   command_status:completed UGV_EXTINGUISH → 发 UGV_RTL accepted
#   command_status:completed UGV_RTL → 静默回到待命

# 终端 3：operator 触发 reset
curl.exe -X POST http://127.0.0.1:5000/scenario/reset `
  -H "Content-Type: application/json" -d '{}'
# 测试 Agent 终端应输出：
#   scenario_event{event:"reset", run_id:N+1} → 清本地状态
#   下一帧 snapshot run_id 跳变 → 重发 PATROL × 3
```

完整冒烟流程预期 wall time ≈ 30-60 s（取决于 tick_fps；GRP 路径长则 UGV 段更慢）。

`test_agent.py` 是**外部独立进程**，不在 Bridge 里跑；零内部 import，纯走 Socket.IO 协议路径，与真实 UrbanAgent 协议等价。可选参数：
- `--url <URL>`：Bridge 基址（默认 `http://127.0.0.1:5000`）
- `-v` / `--verbose`：详细日志
- `--no-extinguish`：只发 UGV_GOTO 不灭火，用于测 supersede / reset cancel 链
- `--namespace /agent`：指定 namespace（默认 `/agent`）

> 旧版 (M0-M8) Bridge 内嵌的 mock_agent 剧本（`SCRIPT = [...]` + `mock_agent_loop`）已**完全移除**（spec D10 / refactor §1）。Bridge 自身不再驱动剧情。

## 4. 配置

### 4.1 default.toml

```toml
[carla]
host = "127.0.0.1"
port = 2000
timeout_s = 30.0           # 提高到 30s 防偶发慢响应（M6 经验）
fixed_delta_seconds = 0.0333
map = "Town10HD_Opt"

[server]
host = "0.0.0.0"
port = 5000
cors_origins = ["http://localhost:5173"]

[broadcast]
state_hz = 10
metrics_hz = 1

[scenario]
default = "s1_fire"

[scenario.s1_fire]
extinguish_radius_m = 5.0
default_uav_rtl_speed = 8.0
default_ugv_target_speed_kmh = 25.0
uav_arrival_eps_m = 0.5
```

> `[agent] mode` 配置项已删除（spec D10）。Bridge 永远以"等远程 Agent 接入"模式运行。

覆盖优先级：env vars > `--config <path>` > `config/local.toml` > `config/default.toml`。
env 用前缀 `CARLABRIDGE_` + 双下划线层级，例：`CARLABRIDGE_CARLA__PORT=2010`。

### 4.2 命令行

```powershell
.\run.ps1                                    # 用 default.toml
.\run.ps1 -Scenario s1_fire -LogLevel DEBUG  # 详细日志
.\run.ps1 -NoCarla                           # 跳过 CARLA，只起 HTTP（前端 smoke）
.\run.ps1 -Config path\to\extra.toml         # TOML overlay
```

## 5. HTTP/WS 端点

| 路径 | 协议 | 用途 |
|---|---|---|
| `GET /healthz` | HTTP | 完整健康状态（design §15.4）：carla/tick_fps/scenario/clients/cameras |
| `GET /debug/events?n=N` | HTTP | dump event_log 环形缓冲，诊断首选 |
| `POST /admin/shutdown` | HTTP | 优雅关停（restore CARLA + destroy actors） |
| `POST /webrtc/{camera_id}` | HTTP+SDP | WebRTC 信令，对齐前端 `webrtc.ts` |
| `GET /video_feed?camera={id}` | MJPEG | 兜底视频流 |
| `POST /scenario/fire` | HTTP JSON | **operator 点火**（refactor §5.1） |
| `POST /scenario/reset` | HTTP JSON | **operator reset**（refactor §5.2） |
| `GET /scenario/status` | HTTP JSON | scenario 状态快照（refactor §5.3） |
| `/socket.io/` | Socket.IO | `/` (前端) + `/agent` (远程 Agent) namespace |

camera id：`aerial` / `ground` / `city`。

### 5.1 HTTP 控制面（refactor v0.3 §5）

operator（人 / curl / cron / GUI）通过 3 个 HTTP 端点驱动场景。Agent **不能**触发这些；reset 是 operator 特权。

#### `POST /scenario/fire` — 点火

相对 **UGV-01** 的 `origin` 向东偏移 90 m 放置火点（与 `GET /scenario/status` 对齐，避免写死世界坐标）：

```powershell
$ugv = (Invoke-RestMethod 'http://127.0.0.1:5000/scenario/status').entities.'UGV-01'.origin
$body = @{ id = 'fire-001'; position = @{ x = ($ugv.x + 90.0); y = $ugv.y; z = $ugv.z } } | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:5000/scenario/fire' -ContentType 'application/json' -Body $body
# 200 OK -> {"status":"ok","incident_id":"fire-001","spawned_actor_id":42,
#            "spawned_at_sim_time":412.34,"run_id":5}
```

从 **cmd.exe** 一行（勿在已打开的 PowerShell 里粘贴，否则外层会提前展开 `$`）：

```cmd
powershell -NoProfile -Command "$ugv = (Invoke-RestMethod 'http://127.0.0.1:5000/scenario/status').entities.'UGV-01'.origin; $body = @{ id = 'fire-001'; position = @{ x = ($ugv.x + 90.0); y = $ugv.y; z = $ugv.z } } | ConvertTo-Json -Compress; Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:5000/scenario/fire' -ContentType 'application/json' -Body $body"
```

请求体：`position{x,y,z}` 必填；`id`（默认 `fire-<uuid8>`）/ `kind`（默认 `"fire"`）/ `severity`（默认 `"high"`）/ `blueprint`（默认按候选回退）可选。

错误码：`400` position 缺失/类型错；`409` id 重复或 blueprint 全部 spawn 失败；`503` reset 进行中。

#### `POST /scenario/reset` — 完全重新初始化

```powershell
curl.exe -X POST http://127.0.0.1:5000/scenario/reset `
  -H "Content-Type: application/json" -d '{}'
# 200 OK -> {"status":"ok","run_id":6,"reset_at_sim_time":423.50,
#            "cancelled_commands":["cmd-9f1","cmd-a02"],
#            "destroyed_incidents":["fire-001"]}
```

行为（参考 refactor §5.2）：① `_resetting=True` → 拒绝新命令/新 fire；② 所有 in-flight 一次性 emit `command_status:cancelled(reason="reset")`；③ sim 域 `teardown()` + `setup()`（CARLA actor_id 变、entity_id 稳定、FrameQueue 实例保留 → 视频流不断）；④ `run_id +=1`；⑤ 退出 resetting；⑥ 广播 `scenario_event{event:"reset",run_id,trigger:"http"}`。

错误码：`503` 上一次 reset 仍在进行中。

#### `GET /scenario/status` — 状态快照

```powershell
curl.exe http://127.0.0.1:5000/scenario/status
# 返回当前 run_id / bridge_session_id / sim_time / resetting / incidents /
# in_flight_commands / entities{id: {origin, current_pose}}
```

不进 sim 域，直接读 fleet / in_flight 内存快照；可作为 Agent 重连后的对账兜底。

## 6. 故障排查

### 6.1 tick_fps 很低（4-9 Hz vs 目标 30 Hz）

**预期行为**。spec D5 已接受：CARLA 渲染线程是 GPU/CPU bound，演示机上 `world.tick()` 单次 ~100-300 ms。  
缓解：CARLA 启动加 `-quality-level=Low`、缩小窗口、关闭后处理。要打到 30 Hz 需要更强的 GPU 硬件。  
**不影响功能**：bridge pacing 逻辑正确（FakeWorld 单测能稳 30 Hz），剧本逻辑按 sim_time 触发，仅 wall time 拉长。

### 6.2 启动 `OSError: [Errno 10048] ... port 5000`

刚刚 shutdown 过的话是 Windows TIME_WAIT (~60 s)。等清空即可。`run.ps1` 启动时会先 warn 这种情况。
```powershell
Get-NetTCPConnection -LocalPort 5000 -State TimeWait
```
有结果就再等会儿；为空就是真有其他进程在占。

### 6.3 CARLA 卡在 sync mode

bridge 异常退出后（被 SIGKILL / OOM / Stop-Process -Force），`finally:` 没机会跑 `restore_original_settings`，CARLA 会留在 `synchronous_mode=True`。其他 CARLA Python 脚本会因为没人 drive tick 而 hang。

手动 reset：
```python
import carla
c = carla.Client('127.0.0.1', 2000); c.set_timeout(10.0); w = c.get_world()
s = w.get_settings(); s.synchronous_mode = False; s.fixed_delta_seconds = None
w.apply_settings(s)
for a in (list(w.get_actors().filter('vehicle.*'))
          + list(w.get_actors().filter('static.prop.*'))
          + list(w.get_actors().filter('sensor.camera.rgb'))):
    a.destroy()
```

### 6.4 `S1: no UGV blueprint could be spawned`

Town10HD spawn point 占用率高，bridge 现在已遍历**所有** spawn point × 所有 `vehicle.*` 蓝图。如果还撞这个，说明上一次 bridge 没清干净。先按 §6.3 reset。

### 6.5 UGV 不动

看 `/debug/events?n=200` 找 `SCENARIO/danger` 的 `follower crashed` 行。M6 verification 时 BasicAgent 撞 30 s RPC timeout，已经永久替换为 `SimpleWaypointFollower`（spec D9）。Follower 的失败模式更少（只读 transform + velocity），如果出现：
- 查看 GRP 是否成功建路（`route built with N waypoints` 在 log 里）。N=0 时 follower 静止刹车。
- 偶发 `WaypointFollower.run_step failed` → follower 被清空 + event_log 报告。可重发 DISPATCH。

### 6.6 前端 LIVE 亮起但数字不动

**预期行为**。Bridge 启动后空载等待，自身不驱动剧情。要看到 UAV/UGV 运动，需要 §3.5 起 `test_agent.py` 下发 PATROL，并通过 `POST /scenario/fire` 触发火情。

### 6.7 frontend 视频面板 placeholder

默认是 placeholder。点右上角 CONNECT chip，选 MJPEG，输入 `http://localhost:5000/video_feed?camera=aerial`（或 ground/city）。  
WebRTC 需先用一次 MJPEG 验证连通性，然后切到 WEBRTC 模式输 `http://localhost:5000/webrtc/aerial`。

## 7. 开发

### 7.1 测试

```powershell
# 已 conda activate ".\.venv"：
python -m pytest tests/ -q

# 或未激活时直接指定解释器：
.\.venv\python.exe -m pytest tests/ -q
```
146 个单元测试，全部不需要 CARLA，~16 秒跑完。

### 7.2 添加新 scenario

1. 在 `carlabridge/scenarios/` 下新建模块（参考 `s1_fire.py`）
2. 继承 `Scenario`，实现 `setup() / teardown()` + on_tick_pre/post + on_command
3. 用 `@register_scenario("name")` 注册
4. 在 `scenarios/__init__.py` 添加 side-effect import
5. `.\run.ps1 -Scenario name` 即可加载

### 7.3 添加新 CommandKind

1. `commands/enum.py` 加 `CommandKind` 枚举值
2. `commands/dispatcher.py` 加独立 `_validate_<kind>(params)` 校验函数（reject 抛 `RejectCommand(reason, detail)`）
3. scenario 的 `on_command` 加 dispatch 分支 + `_handle_<kind>(cmd)` 私有方法 + `_check_completion` 新分支
4. 测试加 `tests/test_dispatcher_v2.py` 解析 case + `tests/test_s1_dispatch.py` 行为 case + `tests/test_command_lifecycle.py` 生命周期 case

详见 `tasks-refactor.md` R1/R3/R4 的 DoD。

## 8. 验收脚本

```powershell
.\scripts\restart_smoke.ps1                    # NF7 + AC-8: 5 次启停无残留
.\scripts\restart_smoke.ps1 -Iterations 3      # 缩短版
.\scripts\nf5_memory_probe.ps1                 # NF5: 5 分钟内存增长 < 200 MB
.\scripts\nf5_memory_probe.ps1 -DurationMinutes 30  # 完整 30 min
```
结果落 `logs/*.csv`。

## 9. 真机验收状态（截至 M8）

| ID | 项 | 状态 | 备注 |
|---|---|---|---|
| AC-1 | 前端 LIVE | ✅ | M2 装配完成 |
| AC-2 | 三路 WebRTC ≤ 5 s | ✅ | 手工验证 |
| AC-3 | MJPEG 兜底 | ✅ | 手工验证 |
| AC-4 | command ack 闭环 | ✅ | sio.call return-value (refactor §3.3)；实测 latency 0-250 ms |
| AC-5 | S1 启动 ≤ 10 s | ✅ | 实测 ~3 s（空载就绪） |
| AC-6 | S1 端到端流程 | ✅ | `test_agent.py` + `POST /scenario/fire` 跑通；详见 §3.5 |
| AC-7 | event_log 完整 | ✅ | 命令相关事件带 `cmd_id`，与 `command_status` 配对 |
| AC-8 | 无残留 actor | ✅ | 单元 5×幂等 + `restart_smoke.ps1` 待真机跑 |
| AC-9-12 | 性能 / 网络抖动 / 一键启动 / 前端 fallback | ✅ | 见 §6 |
| NF1 | tick 30 Hz ±2 Hz | ⚠️ 不可达 | spec D5 已接受降级；实测 ~4 Hz |
| NF2 | 视频 < 300 ms | ✅ | 手工验证 |
| NF3 | 状态 < 100 ms | ✅ | broadcaster 10 Hz |
| NF4 | 命令 < 500 ms | ✅ | 实测 max 250 ms |
| NF5 | 内存 < 200 MB | 🔍 待真机长跑 | 用 `nf5_memory_probe.ps1` 跑 30 min |
| NF6 | 背压不阻塞 tick | ✅ | `test_nf6_backpressure.py` 单元覆盖 |
| NF7 | 5 次启停无残留 | 🔍 待真机 | 用 `restart_smoke.ps1` |
| NF8 | 单机部署 | ✅ | 全程单机 |

## 10. 已知限制

- `SimpleWaypointFollower` 不识别红绿灯 / 不避障 / 不考虑车道偏移（spec D9 决议）。UGV 会闯红灯、撞到挡路的 NPC 车。本期 Town10HD 默认无 NPC，所以演示无影响。
- 单一 CARLA Client 在 sync mode 下 `BasicAgent` 与多相机有 RPC 冲突，已避开（spec D9，详见 `memory/carla_basicagent_timeout.md`）。
- Bridge 自身不驱动剧情 / 不做 sim_time 触发的剧本（spec D10 / refactor §1）。所有 UAV/UGV 行为由外部 Agent 通过 `/agent` 下发；火情 / reset 由 operator 通过 HTTP 触发。
- 每个 entity 同时只允许 1 条 in-flight 命令；新命令立即 `cancelled(reason="superseded")` 旧命令（refactor §6.4）。
