---
version: 1.1
created_at: 2026-09-11
updated_at: 2026-10-01
last_updated: 校正第 2 节订阅节点统计口径（4 处，3 处生效）；第 7 节由"方向未定稿"改为"已定方向：一个 agent 一个 ctx"，并列出待定项
abstract: 说明 EventService 以事件名作为唯一分组键、缺少 agent/session 隔离与解绑机制，导致多 agent 共享同一实例时订阅互相串扰；列出 src/myagent 内实际被使用的订阅节点与尚未被消费的事件；第 7 节记录已定的改造方向（每个 agent 私有持有一份 EventService 的 ctx 隔离容器）。
---

# EventService 无隔离机制（多 agent 注册相互影响、且无清理）

## 版本

| 版本 | 更新内容 |
| ---- | -------- |
| 1.0 | 创建文档初稿 |
| 1.1 | 校正第 2 节订阅节点统计；第 7 节补入已定方向（ctx 隔离容器）与待定项 |

- 状态：`acknowledged`（长期存在的设计限制，非 bug）

## 1. 问题描述

[service.py](file:///d:/desktop/软件开发/agent/src/myagent/infra/events/service.py) 中的 `EventService` 是一个进程内的共享事件总线，它的订阅表结构是：

```python
_hooks: defaultdict[str, list[tuple[weakref.ReferenceType, str]]]
```

即 **仅以事件名（`event_name`）作为分组键**。`emit` / `waterfall` 派发时，只按事件名取出该名字下的**全部**订阅者并逐个调用，派发链路中**不携带任何 agent / session 身份**，`register` 也不记录注册来源属于哪个 agent。

由此产生两个相互关联的限制：

1. **没有隔离机制**：只要多个 agent（或同一 agent 的多个会话）共享同一个 `EventService` 实例，它们对同一事件名的订阅就会落进同一张列表。任何一个 agent 触发事件，都会派发给**所有** agent 注册的回调。
   - 例：agent A 的 `turn/end` 会触发 agent B 的 `ToolRegister.reset_breaker`，反之亦然——A 的轮次结束会清空 B 的工具熔断状态。
   - 例：两个会话共享总线时，任一 `session.append` 触发的 `session/event` 会让**每个** `SessionPresist` 都把记录写进自己的持久化 buffer，造成会话文件互相污染。
   - 该串扰是**静默**的：没有报错、没有告警，只体现为难以定位的数据/状态错乱。

2. **没有清理机制（Agent 生命周期管理缺失）**：`EventService` 没有 `off` / `unregister` 接口，无法在组件仍存活时主动注销订阅；`ReActAgentLoop` 在构造时注册 `turn/end` 回调（见下），但在 `cancel()` / loop 结束 / agent 销毁时**从不解绑**。
   - 目前唯一的回收途径是 `WeakMethod` 弱引用：订阅方对象被 GC 后注册项自动失效，`_clear_dead_callback` 在每次派发前惰性清理。这只能覆盖"对象已死"的情形。
   - 当调用方仍持有组件引用（如长期存活的注册表）时，注册项就一直有效，已停止的 agent 仍会收到其他 agent 的事件。
   - 由于订阅项不带 owner/scope 标识，也**无法按 agent 定向清理**。

> 说明：本文件是"当前系统受什么约束 + 为什么这样"的现状描述；已定的改造方向见第 7 节，落地细节（ctx 的字段、各组件的构造签名变化）属技术债/后续任务，不在本文件展开。

## 2. 当前实际被使用的订阅节点

`src/myagent` 内 `register(...)` 调用共 4 处，其中 3 处生效：

| 事件名 | 订阅回调 | 注册位置 | 作用 |
| ------ | -------- | -------- | ---- |
| `turn/end`（`TURN_END`） | `ToolRegister.reset_breaker` | [loop.py:169](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/agent/loop.py#L169) | 每轮 turn 结束清空工具熔断状态（loop 同时持有 `event_service` 与 `tool_register`，接线在此） |
| `session/event`（`SESSION_EVENT`） | `SessionPresist.cache_data` | [persistence.py:30](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/session/persistence.py#L30) | `session.append` 触发的记录进入持久化 buffer，供异步落盘 |
| `tool/result`（`TOOL_RESULT`） | `ToolEvaluate._on_tool_result` | [tool_evaluate.py:146](file:///d:/desktop/软件开发/agent/src/myagent/evaluate/tool_evaluate.py#L146) | 工具调用结果写入评估 CSV；当前无生产调用方 |

另有一处不生效：[loopcopy.py:80](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/agent/loopcopy.py#L80) 注册了与 `loop.py` 相同的 `turn/end` 回调，但 `loopcopy` 在 `src/` 与 `tests/` 中零引用，属遗留副本。

> [loop.py:169](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/agent/loop.py#L169) 正是"当前被使用"的典型订阅节点：它在 `ReActAgentLoop.__init__` 中注册，只要该 loop 与别的 agent 共用同一个 `EventService`，就会与其他 agent 的 `turn/end` 订阅互相影响。

> 补充：`src/myagent` 内没有 agent 的装配入口（不存在 `EventService()` / `ReActAgentLoop(...)` 的构造点），实际注册由 myagent 之外的调用方完成。因此"一个实例只服务一个 agent"这条约定在 myagent 内部无法被强制，只能由调用方保证。

## 3. 命中"已触发但无订阅方"的事件（当前未被使用）

[eventspec.py](file:///d:/desktop/软件开发/agent/src/myagent/infra/events/eventspec.py) 中登记的事件，除第 2 节表中三处外，其余事件在 `loop.py` / `session.py` 中被 `emit`，但**没有任何订阅方**，触发后实际为空转（`emit` 时命中"未注册"直接返回）：

- `turn/start`、`step/start`、`request/header`、`user/message`
- `assistant/chunk`、`assistant/message`
- `tool/call`
- `step/end`

> `tool/result` 不在此列：`ToolEvaluate` 构造时会订阅它（[tool_evaluate.py:146](file:///d:/desktop/软件开发/agent/src/myagent/evaluate/tool_evaluate.py#L146)）。但当前 `src/myagent` 内没有构造 `ToolEvaluate` 的地方，故实际运行中它仍为空转——是"有订阅方、但订阅方未实例化"。

需要单独说明的是 `request/error`（waterfall 语义）：它同样没有订阅方，但**"无订阅方时 `waterfall` 返回 `None`"这一行为被显式依赖**——[loop.py:856](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/agent/loop.py#L856) 取回后 `or {}` 归一为空裁决，[loop.py:900-902](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/agent/loop.py#L900-L902) 把"决策为 None"解释为"错误无人认领"并抛出 `AgentUnclaimedError`（此前由 [loop.py:1002-1009](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/agent/loop.py#L1002-L1009) 落一条 `agent/error-handle`，decision 记 `unclaimed`）。因此它是"无订阅方"而非"未被使用"，改造事件机制时不可误删该语义。

这也意味着：当前事件总线上的**大部分容量是空置的**，隔离/清理问题尚未在多订阅者的复杂场景下暴露，但一旦按设计目标接入更多订阅（可观测性、评估等），串扰与清理缺失会立刻成为阻塞点。

## 4. 影响范围与严重程度

- **影响范围**：所有"多 agent / 多会话共享同一 `EventService` 实例"的用法。单个 agent + 单个会话（各自 `new EventService`）不受影响。
- **严重程度**：中。当前尚未有正式的多 agent 并行共享入口，但一旦共享即发生**静默串扰**，且没有清理手段，排查成本高。
- **现状规避**：测试中通过"每个会话各自新建 `EventService`"规避，见 [test_loop_e2e.py](file:///d:/desktop/软件开发/agent/tests/e2e/test_loop_e2e.py#L114-L123)（注释明确写道"两个会话共享同一条总线会互相把记录写进对方的持久化 buffer"）。

## 5. 当前假设（系统依赖的脆弱前提）

1. **一个 `EventService` 实例只服务一个 agent / 会话**。这是当前正确运行的前提，但**没有任何代码强制**，完全依赖调用方自律（测试里是"每次新建"的约定）。
2. **订阅方不再需要时即被 GC**。只有这样才能靠弱引用自动清理；若调用方长期持有组件引用，注册项将持续有效并继续接收其他 agent 的事件。
3. **同一事件名对应的订阅语义唯一**。在 `src/myagent` 内，目前每个事件名只有一个订阅者，所以"顺序不可控"问题尚未显现；一旦同一事件出现多个不同语义的订阅者，派发顺序与串扰都会成为问题（[service.py](file:///d:/desktop/软件开发/agent/src/myagent/infra/events/service.py#L11-L25) 顶部注释已记录相关的顺序/解绑问题）。

## 6. 触发条件

- 多个 agent / 会话共用同一个 `EventService` 实例；
- 在同一进程内用父组件的 `EventService` 构造新 agent（复用而非新建）；
- 对同一事件名出现第二处、不同语义的注册；
- agent 被 `cancel()` / 结束 / 销毁后，其订阅方对象仍被外部引用而未 GC。

## 7. 临时方案与计划改进

### 7.0 临时方案（当前）

让每个 agent / 会话各自持有独立的 `EventService` 实例，禁止跨 agent 复用（当前测试即如此规避）。这条约定由调用方自律，myagent 内无从强制（见第 2 节的补充）。

### 7.1 已定方向：一个 agent 一个 ctx

改造方向是把 7.0 那条自律约定**结构化**：为每个 agent 建立一个上下文对象（ctx），**ctx 私有持有一份 `EventService`，该 agent 的全部订阅都注册在这份总线上**。

方向参照 DSH 的 [agent-scope-contexts](file:///d:/desktop/软件开发/deepseek-harness/.agents/notes/implemented/architecture/2026-07-08-agent-scope-contexts.md)（每个活着的 agent 拥有一层扁平注册层 `agent.ctx`，带 scope 的服务把**部署全局层**与该 agent 的一层合并成操作视图；事件默认只到达 unscoped 监听器与同 agent 监听器）。

本项目只取其一半：**保留 agent 层，去掉部署全局层**。带来的直接后果：

- 不存在"全局 ∪ 当前层"的并集运算，也就没有它的操作对象；
- 不存在 unscoped 监听器可以收听全部 agent 的位置；
- agent 之间互不互通。

代价：跨 agent 的观测 / 评估组件（如 `ToolEvaluate`）在总线内没有立足点，需由装配方显式聚合多个 ctx，或改为不经总线。`src/myagent` 内当前没有这类活的消费者，代价暂不兑现。

### 7.2 ctx 的职责与内容

ctx 定位为**隔离容器**（不做 DSH 那样的插件化）：

| 内容 | 说明 |
| ---- | ---- |
| `agent_name` | ctx 自身持有，作为该 agent 的标识 |
| `EventService` | ctx 私有持有；该 agent 内全部订阅注册于此，这是隔离的落点 |
| `SystemPrompt` | 提示词装配 / 渲染 |
| `Session`（含 `SessionPresist`） | `SessionPresist` 由独立组件合并进 `Session` |
| 策略 | 重试、熔断、人在回路等 |
| `AgentLoop` | ReAct 循环 |

ctx 对外透传 `emit` / `waterfall`（`register` 同理）三个方法，转发给内部 `EventService`。这三个调用在 loop 与 session 中频率最高，透传后组件写 `ctx.emit(...)`，省掉一层 `self._event_service`。

### 7.3 对 `EventService` 的影响：本文件所述机制不改

这是本方向最反直觉的一点——**`EventService` 自身一行都不改**：

- `_hooks` 保持单层（[service.py:52](file:///d:/desktop/软件开发/agent/src/myagent/infra/events/service.py#L52) 的 `defaultdict[str, list]`）；
- `emit` / `waterfall` 签名不变，[loop.py](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/agent/loop.py) 内约 10 处调用点无需改动；
- 不需要补 `off`：ctx 连同它私有的 `EventService` 一起被丢弃引用即可回收，第 1 节的"无清理机制"随之消解；
- `_clear_dead_callback` 的单层遍历不变。

改动只落在**"谁持有 `EventService`"**：从现状的"每个调用方各自 `new`、靠自律"变成"由 ctx 持有、随 agent 一起生存"。

> 对照：另一条曾经设想的路径是"给 `EventService` 加 agent 维度的字典 / scope 键"，需要 `register`、`emit`、`waterfall` 全部加参数，并补一套按 agent 定向清理的 `off`。ctx 方案把改动降为"改持有关系"，故不采用。

### 7.4 落地后本文件所述限制的消除情况

| 本文件所述问题 | 落地后 |
| -------------- | ------ |
| 第 1 节限制 1：多 agent 订阅互相串扰 | 消除。总线为 ctx 私有，A 的事件到不了 B |
| 第 1 节限制 2：无清理机制、无解绑接口 | 消除。ctx 被丢弃即整体回收 |
| 第 5 节假设 1：一个 `EventService` 只服务一个 agent | 由调用方自律变为结构保证 |
| 第 5 节假设 2：订阅方不再需要时即被 GC | 仍然成立（`WeakMethod` 机制不变），但不再是隔离的唯一依赖 |
| 第 5 节假设 3：同一事件名订阅语义唯一 | 不变（`session/event` 已存在多个语义不同的订阅方） |

### 7.5 待定项

落地前需要确定：

1. `SessionPresist` 合并进 `Session` 后，落盘路径从何而来（现状由 `SessionStore._session_file` 算好后注入，见 [store.py:45-47](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/session/store.py#L45-L47)、[store.py:61](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/session/store.py#L61)）；
2. ctx 类的命名与位置：现有 [app_context.py](file:///d:/desktop/软件开发/agent/src/myagent/core/app_context.py) 的注释是"一个 app 的全局上下文"，与"一个 agent 一个 ctx"的定位相反；且 `src/myagent/core/` 目录下只有这一个文件；
3. `SystemPrompt` 的 `agent_name` 维度（[systemprompt.py:16](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/systemprompt/systemprompt.py#L16)）在 ctx 模型下退化为单桶，是否随之简化；
4. ctx 的生命周期归属：由装配方显式收尾，还是依赖引用回收。现状 `loop.cancel()`（[loop.py:196](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/agent/loop.py#L196)）只清收件箱，不做销毁。

> 说明：本文件按 known-limitations 的定位只记录"当前约束 + 已定方向 + 为什么这样"，落地细节（ctx 的字段、各组件的构造签名变化）不在本文件展开，方案定稿后另立 technical-debt / plan 文档。

## 8. 相关文档

- [service.py](file:///d:/desktop/软件开发/agent/src/myagent/infra/events/service.py)（顶部注释：订阅顺序不可控、解绑接口缺失）
- [eventspec.py](file:///d:/desktop/软件开发/agent/src/myagent/infra/events/eventspec.py)（事件登记表，改造时的事件全集）
- [loop.py:169](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/agent/loop.py#L169)、[persistence.py:30](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/session/persistence.py#L30)、[tool_evaluate.py:146](file:///d:/desktop/软件开发/agent/src/myagent/evaluate/tool_evaluate.py#L146)（当前生效的三个订阅节点）
- [store.py](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/session/store.py)（`SessionPresist` 的组装点，第 7.5 节待定项 1 相关）
- [app_context.py](file:///d:/desktop/软件开发/agent/src/myagent/core/app_context.py)（第 7.5 节待定项 2 相关）
- [DSH agent-scope-contexts](file:///d:/desktop/软件开发/deepseek-harness/.agents/notes/implemented/architecture/2026-07-08-agent-scope-contexts.md)（第 7.1 节方向的参照实现，外部仓库）
