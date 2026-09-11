---
version: 1.0
created_at: 2026-09-11
updated_at: 2026-09-11
last_updated: 创建 known-limitations 目录索引与文档格式说明
abstract: known-limitations 目录索引；收录当前系统"这样运作是有原因的"已知限制文档，并提供本目录文档的固定格式约定。
---

# known-limitations 索引

本目录收录**已知限制**类文档：描述"当前系统就是这样运作的，这是设计选择或现实约束，不是 bug"。判定标准、消费场景与写作视角见 `docs/docs-rules/known-limitations-and-debt-rules.md`。

## 文档格式

每篇 known-limitations 文档必须包含以下字段/章节：

- frontmatter：`version`、`created_at`、`updated_at`、`last_updated`、`abstract`
- 正文开头 `## 版本` 章节（表格）
- 状态：`acknowledged` / `resolved`（长期限制为 `acknowledged`）
- 问题描述
- 影响范围 + 严重程度
- 当前假设（系统依赖的脆弱前提）
- 触发条件
- 临时方案或计划改进
- 相关文档（ADR、调查报告、代码位置等）

## 索引

## event-service-无隔离机制
- updated_at : 2026-09-11
- path: `docs/known-limitations/2026-09-11-event-service无隔离机制.md`
- 触发规则：改动 `EventService` 订阅/派发逻辑、多 agent 共享事件总线、或排查 agent 间状态串扰时阅读
- 内容摘要：EventService 仅以事件名分组、无 agent/session 隔离与解绑机制，多 agent 共享实例时订阅互相串扰；列出当前生效的两个订阅节点（`turn/end`、`session/event`）与已触发但无订阅方的事件
