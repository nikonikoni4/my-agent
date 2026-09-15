---
version: 1.0
created_at: 2026-09-15
updated_at: 2026-09-15
last_updated: 创建文档初稿
abstract: ReActAgentLoop 的错误处理重构为「2 个 except + finally 内三阶段（补全/记账/决策）」；终止统一以抛出表达（turn 由异常得知终态），预算检查并入同一决策路径并以 step_opened 保证 step 配对，每次失败在发生点落 llm/retry
status: decided
---

# agent loop 错误处理重构

## 版本

| 版本 | 更新内容 |
| ---- | -------- |
| 1.0 | 创建文档初稿 |

## 问题界定

### 问题简述

旧 loop（`loop copy.py`）把错误处理分散在四个位置：`except` 分支里判断错误种类、`finally` 里的 `_handle_step_error` 做重试决策、`turn` 里用返回值把终态归一化、`step_limit` 超限则就地 `break`。结果是：

- **终态可能失真**：异常路径上 `turn` 只能拿到默认的 `success`；`step/end` 的 `reason_type` 同样可能记成默认值；
- **错误信息只有字符串**：`reason_text` 记的是 `str(异常)`，看不出类别；"无人认领"时还会被 `LLmError("错误无人认领")` 把真实错误顶掉；
- **改策略要动多处**：策略（重试/退避/终止）散在 loop 主体里，与"循环只做编排"的定位相悖。

### 讨论范围

- 捕获与决策的结构形态（几个 `except`、决策点放哪）。
- 终止的表达方式（抛出 vs 返回值）。
- 记账内容（`step/end`、`turn/end`）与错误信息形态。
- 重试退避、发生点记录（`llm/retry`）的职责归属。

### 非讨论范围

- 错误类型树的分域——见《异常分类树新增策略域与未知域》。
- session 错误记录的字段与层级——见《session 错误信息记录策略》。
- 各错误类别的具体处置策略（人在回路、降级等）——后续独立决策。
- 未决项：`FinalResult.error_type` 的最终落点、`AgentUnknownError` 是否改名为 `AgentUnclaimedError`。

### 模糊信息的明确定义

- **阶段**：`finally` 内按序执行的三段——补全 / 记账 / 决策。
- **收尾**：不进入重试、直接终止本次 turn 的处置（`unclaimed` / `exhausted`）。

### 问题深度

涉及"错误处理如何穿越分层、循环主体能保持多干净"的架构原则，属长期维护方式决策。

## 现状

- 旧实现中，`step` 的 `finally` 里调用 `_handle_step_error`：它负责触发 `request/error`、判重试次数、写 `llm/retry`、退避等待；`turn` 用一个 `StepOut`/`FinalResult` 把终态带回 `turn/end`。
- `step_limit` 的检查在 `try` 之外，超限时就地造 `RuntimeError` 并 `break`，不进入上述任何一段。
- `llm/retry` 只记 `retry_count` + `reason`，**不含触发它的错误本身**。
- `Session` 的请求消息由记录派生：`tool/call` 若缺配对的 `tool/result`，消息面会出现"有 tool_calls 却没有 tool 响应"，模型接口会拒。

## 决策前提

前提是**当前成立的事实与约束**（非决策内容）；它们失效时应切换方案（见"决策逻辑"）。

- 前提 1（现状事实）：错误处理分散在四处，改策略要同时动 `except`、`finally`、`turn`、`step_limit` 四处。
- 前提 2（现状事实）：异常路径上 `turn`/`step` 的终态只能取到默认值；错误信息只有 `str(异常)`，且"无人认领"时会覆盖真实错误。
- 前提 3（现状事实）：`tool/call` 缺配对会产生非法消息面，必须补齐才能续跑。
- 前提 4（现状事实）：`step_limit` 检查在 `try` 之外，不进入错误处理；挪进 `try` 后又会与 `step/start` 失配（写出无配对的 `step/end`）。
- 前提 5（实测事实）：当前错误链最长 3 层（`httpx.*` → `LLMConnectionError` → `AgentUnknownError`）；`ExceptionGroup` 的 `__cause__`/`__context__` 均为 `None`，不在链上。
- 前提 6（用户约束）：认证 / 模型 / 接入点这类配置错误，"不重试但继续下一条"没有意义，应停止让用户去改配置。
- 前提 7（用户约束）：当前没有为"终止但不抛"的错误类型想好处置，故该出口先不承担终止。

## 可选方案

### 方案 A：2 个 `except` + `finally` 内三阶段 + 终止统一抛出（选定）

- `step` 只保留 2 个 `except`：取消（`CancelledError`）与兜底（`Exception`），二者都只把错误收进 `step_error`，不做任何判断。
- `finally` 按序做三件事：① `_complete_session` 补全 session；② 写 `step/end`；③ `await _handle_error` 决策。
- 终止统一以**抛出**表达：取消原样上抛、无人认领抛 `AgentUnknownError`、重试耗尽抛 `RetryExhaustedError`；`turn` 用 `except` 得知终态并在 `finally` 写 `turn/end`。
- 预算检查挪进 `try`（从而进入同一决策路径），并用 `step_opened` 保证 `step/start`↔`step/end` 配对。
- 每次失败（含 `unclaimed` / `exhausted`）都在发生点落一条 `llm/retry`。

**优势**

- 循环主体只剩"2 个 except + 1 个 finally"，改策略不动主框架。
- 终态不再依赖返回值，`turn` 在异常路径上也能记出正确的 `turn/end`。
- 六类错误走同一条路径，纪录形态一致。

**劣势**

- `finally` 里出现 `break`，需要靠"分支互斥"保证不吞在途异常（见决策逻辑的防御性说明）。
- 决策点与记账点都在 `finally` 内，读代码时要按序理解三段。

### 方案 B：决策点放在 `try/finally` 之后（否决）

把决策与 `break` 移出 `finally`，放在循环末尾。

**否决理由**：`finally` 必须写完 `step/end` 才能决策（阶段次序要求），而 `break` 一旦落在 `finally` 之外就无法表达"本轮终止"；移出去以后反而要把 `step_error` 透传到循环末尾，多一处状态。

### 方案 C：终止用"返回 `FinalResult`"表达（否决）

`step` 返回终态、`turn` 据此写 `turn/end`。

**否决理由**：异常路径仍需 `turn` 自行兜底（返回值拿不到），等于两套机制并存；且若把异常继续上抛，会带走整个 loop 并丢掉 `inbox` 里排队的消息。

### 方案 D：预算检查留在 `try` 之外（否决）

**否决理由**：它绕过阶段 1/2/3（无补全、无记账、无决策），成为第二个终止出口；若要让它仍走决策，就得在循环顶重复调用一次决策函数。

## 决策逻辑

| 前提条件 | 对应决策 | 前提失效时的切换 |
|----------|----------|------------------|
| 前提 1：分散在四处，改策略动多处 | 收拢为「2 个 except + finally 三阶段」 | — |
| 前提 2：返回值在异常路径取不到终态 | 终止统一以**抛出**表达，`turn` 由异常得知 | — |
| 前提 3：`tool/call` 缺配对会产生非法消息面 | 异常路径下补占位 `tool/result` | 若改为"整轮丢弃、不续跑"，可省去补全 |
| 前提 4：预算检查挪进 `try` 会与 `step/start` 失配 | 用 `step_opened` 保证配对 | 若把预算检查移到 `step/start` 之后，可去掉该标志（代价：留一条空 step 记录） |
| 前提 5：链最长 3 层、group 不在链上 | `reason_text` 用异常链文本：限深 3、展开 group、截断显式标注 | 链变长 → 调整上限或改为结构化承载 |
| 前提 6：配置错误"继续下一条"无意义 | 移除 `dont_retry` 条目，使其落到无人认领 → 停止 | 若某类错误确实"跳过即可继续"，需重新引入对应决策 |
| 前提 7：没有想好"终止但不抛"那类的处置 | 该出口暂不承担终止（语义未定，见 known-limitations） | 一旦新增此类决策值，须同时补"`turn` 如何得知终止" |

**防御性说明**：`finally` 里的 `break` 只有在"无在途异常"时才安全。当前成立，因为：(a) 两个 `except` 都把异常收进了 `step_error`，`finally` 执行时无在途异常；(b) 取消路径由 `_handle_error` 直接 `raise`，走不到 `break`。新增分支时必须保持这两个条件之一。

## 演进历史

| 版本 | 方案 | 解决的问题 | 引入的新问题 |
| ---- | ---- | ---------- | ------------ |
| v1 | `step_error` + `_handle_step_error`（在 finally）+ `turn` 归一化返回值 | 能重试、能记录 | 终态可能失真；错误信息只有 `str`；策略散在四处 |
| v2 | 收拢为「2 个 except + finally 三阶段」 | 单一出口，主框架不再含策略 | 预算检查仍绕过（在 `try` 外）；`step/end` 会落单 |
| v3 | 预算检查并入同一路径 + `step_opened` 保证配对（最终） | 六类错误同一路径、记账配对成立 | 无 |

## 最终决策

当前成立的前提：前提 1~7。

因此采用 `方案 A`：

- `step` 收敛为 2 个 `except`（取消 / 兜底，均只收集）+ 1 个 `finally`（补全 → 记账 → 决策）。
- 终止统一抛出；`turn` 用 `except` 收终态并在 `finally` 写 `turn/end`，不再依赖返回值。
- 预算检查在 `try` 内、`step/start` 之前；`step_opened` 保证不写无配对的 `step/end`。
- `_handle_error` 为 async：取消上抛；无人认领抛 `AgentUnknownError`；`retry`/`backoff_retry` 退避后返回；耗尽抛 `RetryExhaustedError`。
- 每次失败（含两种收尾）先落一条 `llm/retry`，携带 `error_type` + `error_message`。
- `reason_text` 用异常链文本（限深 3、展开 `ExceptionGroup`、截断显式标注）。
- 恢复 `RetryPolicy` + `retry_delay(error, policy, attempt)`（沿用旧名与签名），退避取本地计算与服务端 `Retry-After` 的较大值。

前提失效时的切换路径：见决策逻辑表。

## 决策原因

- 原因 1：前提 1 + 2 下，"循环只做编排、策略集中一处"与"终态必须可靠"同时要求：捕获只收集、决策只在一处、终态由异常传递而不能靠返回值。
- 原因 2：前提 3 下，取消打断工具调用必须补配对，否则续跑会被模型接口拒绝——这不是可选的清理，而是正确性要求。
- 原因 3：前提 4 下，`step_opened` 是"零标志方案"（把预算检查放到 `step/start` 之后）与"零空记录方案"之间的取舍；选标志是因为空 `step` 记录会污染统计（step 计数、每步耗时/token 均值），而统计准确是本项目的核心目标之一。
- 原因 4：前提 5 下，异常链用文本而非结构化嵌套：文本满足"只读日志即可定位"，结构化会引入递归 schema 与消费成本。
- 原因 5：前提 6 + 7 下，`dont_retry` 移除使决策收敛为「继续 / 抛出」两种，不留第三态。

## 后续影响

- 未决项（本 ADR 不含）：`FinalResult.error_type` 的最终落点（当前算出但未写入 `turn/end`）、`AgentUnknownError` 是否改名为 `AgentUnclaimedError`。
- `LLMRetryData` 语义扩展为"失败处置记录"并新增两个字段；**注意 session 加载边界是"字段不符抛异常"**，历史 session 中只有 2 个字段的 `llm/retry` 记录可能加载失败，待验证。
- `llm_retry_count` 的口径随之扩大（`unclaimed`/`exhausted` 也计入）；因二者都是终止、之后不再计算 `attempt`，退避次数不受影响，但**统计 `llm/retry` 条数的地方要同步认知**。
- `_complete_session` 目前是"扫描全量记录、补齐未配对的 `tool/call`"，实现者已标注需要重做（性能与作用域都需要收窄到本轮）。
- 测试待跟进：`tests/agent/core/test_loop_paths.py` 仍引用已改名的 `LLmError`（collection error）；`test_react_loop_breaker.py` 的步数兜底用例断言的是旧行为（就地终止、不外抛），现在会收到 `AgentUnknownError`（其 cause 为 `MaxStepsExceededError`）。
- `break` 作为"终止但不抛"的出口语义未定且当前不可达，另行记录于 known-limitations。
