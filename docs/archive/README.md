# 历史文档归档

本目录保留 CarlaBridge 项目的**历史草案与开发 changelog**。所有内容已被后续重构推翻或落地完成；当前实现以仓库根的 `README.md` / `design.md` / `bridge-agent-protocol-v1.md` 为准。

## 文件清单

| 文件 | 时间线 | 当前状态 |
|---|---|---|
| `spec-v0.1.md` | 项目初版需求规范 | 多数 F4/§7.2/§8/D2 已被 D10 推翻；§1/§2/§4.1/§13 仍可参考；D1/D3/D5/D6/D7/D9/D10 决议仍生效 |
| `design-v0.1.md` | 项目初版架构设计 | §5.3/§8/§9/§10 已 superseded；§3/§4/§7/§11/§14/§17 仍准确（已并入新 `design.md`） |
| `tasks-m0-m8.md` | M0~M8 开发任务清单 | 全部 ✅ 完成；M6 中 AgentLink/mock 任务已被 refactor 删除 |
| `design-refactor-agent-boundary.md` | refactor v0.3 + R11 决议设计 | R1~R11 100% 落地；保留作为决议出处 + 模块改造细则参考 |
| `tasks-refactor-r1-r11.md` | R1~R11 重构任务清单 + DoD | 全部 ✅ 完成；DoD 是当前测试用例的来源 |

## 为什么保留？

- 每份文档都有详尽的**决议依据**和**踩坑记录**（spec D9 BasicAgent timeout 调查、tasks-m0-m8 §M6 真机 debug 11 条、tasks-refactor §14 风险提醒等），重新写新文档时容易漏掉
- 有些决议（D1/D3/D5/D6/D7/D9/D10）当前**仍生效**，但出处只在这里
- 协议演进 v1.x 时回头比对 v0.1 → v1.0 的差异有用

## 不要据此改代码

每份文件的开头都加了 `⚠️ ARCHIVED` 头部，列出哪些章节已 superseded。如果某段描述与 `carlabridge/` 实际代码冲突，**以代码为准**。
