---
version: 1.1
created_at: 2026-09-11
updated_at: 2026-09-15
last_updated: 新增 2026-09-15 step 终止出口（break）语义未定条目
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

## 工具熔断抛错时机与人在回路挂点缺失
- updated_at : 2026-09-11
- path: `docs/known-limitations/2026-09-11-工具熔断抛错时机与人在回路挂点缺失.md`
- 触发规则：改动工具熔断（`raise_on_break` / `_on_failure`）、`request/error` 决策分支、或开始实现人在回路时阅读
- 内容摘要：`raise_on_break` 熔断错误在 loop 写 `tool/result` 之前抛出，导致会话出现无配对的 assistant tool_calls、step 终态可能被记为 success；且 request/error 为同步 waterfall、loop 仅识别 retry 类决策，无法承载人在回路的异步等待。该路径由配置开关挡住，当前默认关闭，改造方向已定

## step 的 break 终止出口语义未定
- updated_at : 2026-09-15
- path: `docs/known-limitations/2026-09-15-step终止出口break语义未定.md`
- 触发规则：改动 `step` 的 `_handle_error` 或策略表决策值（`retry` / `dont_retry` / 新增决策）、或排查 `turn/end` 终态记录失真时阅读
- 内容摘要：`step` 里"终止但不抛异常"的 `break` 出口语义未定（算不算失败、终态由谁写入）；当前因策略表不存在非 retry 决策而只承担"无错误退出"含义、不可达，一旦新增"终止但不抛"的决策值，`turn` 因拿不到异常会把失败的轮次记成 `success`
