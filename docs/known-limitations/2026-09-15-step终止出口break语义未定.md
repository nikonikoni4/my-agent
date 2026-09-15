---
version: 1.0
created_at: 2026-09-15
updated_at: 2026-09-15
last_updated: 创建文档，记录 step 的 break 终止出口语义未定且当前不可达
abstract: step 中"终止但不抛异常"的 break 出口语义未定（算不算失败、终态由谁写入都不确定），当前因策略表不存在非 retry 决策而不可达；一旦新增此类决策，turn/end 会把失败的轮次记成 success
---

# step 的 break 终止出口语义未定，且当前不可达

状态：acknowledged

## 版本

| 版本 | 更新内容 |
| ---- | -------- |
| 1.0 | 创建文档初稿 |

## 问题描述

`ReActAgentLoop.step` 的错误处理收拢在 `finally` 中，出口有两个：

- **抛出**：取消原样上抛；`request/error` 无决策时抛 `AgentUnclaimedError`（`from` 原错误）。
- **`break`**：`_handle_error` 的返回值不是 `"retry"` 时走 `else: break`。

第二个出口的**语义未定**：

- 它算不算"失败"？
- 终态由谁写进 session —— `step` 自己写 `step/end`，还是交给 `turn` 写 `turn/end`？
- `reason_type` 该记 `error` 还是 `interrupted`？

同时它**当前不可达**（见"当前假设"）。

## 影响范围 + 严重程度

- **影响范围**：`ReActAgentLoop.step` / `turn` 的终态记录（`step/end`、`turn/end`）。
- **严重程度**：当前**低**（不可达，无实际影响）；一旦可达即为**记录失真**——`turn` 的终态判定完全依赖 `except` 捕获到的异常，走 `break` 时异常不会到达 `turn`，`turn` 的 `finally` 会用默认值把 `turn/end` 记成 `success`，即"这一轮明明失败了，日志里是成功"。

## 当前假设（脆弱前提）

**策略表（`LLMRerty`）中不存在会返回"非 `retry` 真值决策"的条目。**

具体地：`dont_retry` 一类条目将被全部移除，于是 `_handle_error` 的返回值只可能是：

- `None` —— 本轮无错误（正常结束），以及
- `"retry"` —— 继续下一轮。

两者都不会命中 `else: break` 的"终止"含义，因此该出口目前**只承担"无错误、退出循环"**这一含义。

> 为什么移除 `dont_retry`：认证 / 模型 / 接入点这类配置错误，本 turn 内重试不会变好；应明确停止、让用户去改配置，而不是"放过这条、继续下一条"。

## 触发条件

策略表新增任意一个"**决定终止、但不抛异常**"的决策值（例如恢复 `dont_retry`，或新增 `give_up`），`else: break` 立刻成为真正的终止出口。届时需要一并补上：

- `turn` 如何得知本轮终止（异常之外的第二条通路）；
- `turn/end` 的 `reason_type` 取值规则。

## 临时方案或计划改进

**暂无方案，且有意暂不决定**——当前没有任何错误类型需要它。

将来若需要，两条候选方向（未选定）：

1. `break` 即终止：由 `step` 自己把终态写进 `step/end`，`turn` 通过读 session（或经返回值）得知本轮失败；
2. 废弃 `break` 作为终止出口：终止一律走"抛出"，由 `turn` 的 `except` 统一收口。

## 相关文档

- 决策依据：[session 错误信息记录策略](..\adr\2026-09-15-session错误信息记录策略.md)（终态记录形态：类别 + 异常链文本）
- 相关决策：[异常分类树新增策略域与未知域](..\adr\2026-09-15-异常分类树新增策略域与未知域.md)、[LLM 错误处理分类策略](..\adr\2026-09-09-LLM错误处理分类策略.md)
- 代码位置：[loop.py - step 的 finally 与 break](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/agent/loop.py#L216-L231)、[loop.py - turn 的终态记录（依赖异常）](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/agent/loop.py#L102-L120)
