# CarlaBridge

实时中间件，桥接 CARLA 仿真器、React 数字孪生指挥前端、Urban Agent 调度系统。

- **上游**：CARLA 0.9.16 (Town10HD_Opt)
- **下游**：`D:\urban_frontend` (React + Socket.IO + WebRTC，本期只读)
- **平行**：Urban Agent (本期由 scenario 内的 mock 剧本替代)

## 1. 文档结构

| 文档 | 内容 |
|---|---|
| `spec.md` | WHAT / WHY — 需求规范、决议记录 (D1-D9) |
| `design.md` | HOW — 架构、模块、跨域并发模型、风险 |
| `tasks.md` | 开发拆分 — M0-M8 完成状态、AC/NF 真机进度 |
| `README.md` | 这个文档 — 安装、启动、故障排查 |

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
"<CARLA_ROOT>\CarlaUE4.exe" -quality-level=Low -ResX=1280 -ResY=720
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
- 最后 `tick loop started (delta=0.0333s)` + `mock_agent_loop started`

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
VITE_UAV_FEED_URL=http://localhost:5000/video_feed?camera=aerial
VITE_UGV_FEED_URL=http://localhost:5000/video_feed?camera=ground
VITE_CITY_FEED_URL=http://localhost:5000/video_feed?camera=city
```

变量名错位：前端 `UAV_FEED_URL` ↔ 后端 camera id `aerial`；前端 `UGV_FEED_URL` ↔ `ground`。

### 3.4 你应该看到什么

打开浏览器后约 30-60 秒内（按物理时间）：

| sim_t | wall_t (4Hz) | 事件 | 视觉 |
|---|---|---|---|
| 0 | 0 | 启动，UGV+UAV spawn | LIVE 亮起 |
| 4 | 1s | "patrol started" | EventLog 出现 |
| 6 | 1.5s | "detected fire" | EventLog 红色 |
| 7 | 1.7s | UAV-02/03 RTL | (UAV 已在 origin，no movement) |
| 8 | 2s | UAV-01 HOLD | — |
| 9 | 2.2s | **UGV_DISPATCH** | UGV 开始开向火源 |
| ~22 | ~5.5s | UGV arrived (auto detect) | UGV 停在火源旁 |
| 27 | 6.7s | "fire extinguished" | — |
| 29 | 7.2s | UGV_RTL (222 waypoints) | UGV 调头返航 |
| 45 | 11s | "UGV returned" (mock) | (剧本写死时间) |
| 46 | 11.5s | "scenario complete" | — |

实际 wall time = sim_t × (30/tick_fps)。tick_fps 当前实测 ~4 Hz，所以全剧本约 6 分钟。

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

[agent]
mode = "mock"              # "mock" | "remote"

[scenario]
default = "s1_fire"
```

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
| `/socket.io/` | Socket.IO | `/` (前端) + `/agent` (远程 Agent) namespace |

camera id：`aerial` / `ground` / `city`。

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

可能是 mock 剧本还没到第一个事件 (sim_time=4)。`tick_fps=4 Hz` 时大约 wall=1 秒。耐心等。

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

1. `commands/enum.py` 加枚举值
2. `commands/dispatcher.py:_validate_payload` 加 schema 校验（如需）
3. scenario 的 `on_command` 实现新分支
4. 测试加 `test_commands.py` 解析 case + `test_s1_command.py` 行为 case

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
| AC-4 | mock command ack 闭环 | ✅ | 实测 latency 0-250 ms |
| AC-5 | S1 启动 ≤ 10 s | ✅ | 实测 ~3 s |
| AC-6 | S1 11 步全自动跑完 | ✅ | 实测一次完整剧本 6 分钟 wall |
| AC-7 | event_log 完整 | ✅ | 11 步事件全 event_log 记录 |
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
- mock 剧本 `at=45` (UGV returned) 是写死时间，UGV 实际返航可能更晚（spec §17 风险）；mock event 与物理状态本就解耦。
- 单一 CARLA Client 在 sync mode 下 `BasicAgent` 与多相机有 RPC 冲突，已避开（spec D9，详见 `memory/carla_basicagent_timeout.md`）。
