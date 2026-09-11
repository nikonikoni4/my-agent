---
version: 1.0
created_at: 2026-09-11
updated_at: 2026-09-11
last_updated: 创建文档，记录 EventService 无隔离机制与无清理机制的已知限制
abstract: 说明 EventService 以事件名作为唯一分组键、缺少 agent/session 隔离与解绑机制，导致多 agent 共享同一实例时订阅互相串扰；列出当前实际被使用的订阅节点与尚未被消费的事件。
---

# EventService 无隔离机制（多 agent 注册相互影响、且无清理）

## 版本

| 版本 | 更新内容 |
| ---- | -------- |
| 1.0 | 创建文档初稿 |

- 状态：`acknowledged`（长期存在的设计限制，非 bug）

## 1. 问题描述

[service.py](file:///d:/desktop/软件开发/agent/src/myagent/infra/events/service.py) 中的 `EventService` 是一个进程内的共享事件总线，它的订阅表结构是：

```python
_hooks: defaultdict[str, list[tuple[weakref.ReferenceType, str]]]
```

即 **仅以事件名（`event_name`）作为分组键**。`trigger` / `emit` / `waterfall` 派发时，只按事件名取出该名字下的**全部**订阅者并逐个调用，派发链路中**不携带任何 agent / session 身份**，`register` 也不记录注册来源属于哪个 agent。

由此产生两个相互关联的限制：

1. **没有隔离机制**：只要多个 agent（或同一 agent 的多个会话）共享同一个 `EventService` 实例，它们对同一事件名的订阅就会落进同一张列表。任何一个 agent 触发事件，都会派发给**所有** agent 注册的回调。
   - 例：agent A 的 `turn/end` 会触发 agent B 的 `ToolRegister.reset_breaker`，反之亦然——A 的轮次结束会清空 B 的工具熔断状态。
   - 例：两个会话共享总线时，任一 `session.append` 触发的 `session/event` 会让**每个** `SessionPresist` 都把记录写进自己的持久化 buffer，造成会话文件互相污染。
   - 该串扰是**静默**的：没有报错、没有告警，只体现为难以定位的数据/状态错乱。

2. **没有清理机制（Agent 生命周期管理缺失）**：`EventService` 没有 `off` / `unregister` 接口，无法在组件仍存活时主动注销订阅；`ReActAgentLoop` 在构造时注册 `turn/end` 回调（见下），但在 `cancel()` / loop 结束 / agent 销毁时**从不解绑**。
   - 目前唯一的回收途径是 `WeakMethod` 弱引用：订阅方对象被 GC 后注册项自动失效，`_clear_dead_callback` 在每次派发前惰性清理。这只能覆盖"对象已死"的情形。
   - 当调用方仍持有组件引用（如长期存活的注册表）时，注册项就一直有效，已停止的 agent 仍会收到其他 agent 的事件。
   - 由于订阅项不带 owner/scope 标识，也**无法按 agent 定向清理**。

> 说明：本文件是"当前系统受什么约束 + 为什么这样"的现状描述；对应的改造方向（如何加隔离、补 `off`）属于技术债/后续任务，不在本文件展开。

## 2. 当前实际被使用的订阅节点

全仓库（`src/`）中 `event_service.register(...)` 仅有两处，即当前真正生效的订阅：

| 事件名 | 订阅回调 | 注册位置 | 作用 |
| ------ | -------- | -------- | ---- |
| `turn/end`（`TURN_END`） | `ToolRegister.reset_breaker` | [loop.py](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/agent/loop.py#L79-L81) | 每轮 turn 结束清空工具熔断状态（loop 同时持有 `event_service` 与 `tool_register`，接线在此） |
| `session/event`（`SESSION_EVENT`） | `SessionPresist.cache_data` | [persistence.py](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/session/persistence.py#L30) | `session.append` 触发的记录进入持久化 buffer，供异步落盘 |

> `[loop.py#L81](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/agent/loop.py#L81)` 正是"当前被使用"的典型订阅节点：它在 `ReActAgentLoop.__init__` 中注册，只要该 loop 与别的 agent 共用同一个 `EventService`，就会与其他 agent 的 `turn/end` 订阅互相影响。

## 3. 命中"已触发但无订阅方"的事件（当前未被使用）

[eventspec.py](file:///d:/desktop/软件开发/agent/src/myagent/infra/events/eventspec.py) 中登记的事件，除上表两个外，其余事件在 `loop.py` / `session.py` 中被 `trigger`，但**没有任何订阅方**，触发后实际为空转（`emit` 时命中"未注册"直接返回）：

- `turn/start`、`step/start`、`request/header`、`user/message`
- `assistant/chunk`、`assistant/message`
- `tool/call`、`tool/result`
- `step/end`

需要单独说明的是 `request/error`（waterfall 语义）：它同样没有订阅方，但**"无订阅方时 `trigger` 返回 `None`"这一行为被显式依赖**——[loop.py](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/agent/loop.py#L397-L402) 将 `None` 解释为"错误无人认领"并抛出 `LLmError`。因此它是"无订阅方"而非"未被使用"，改造事件机制时不可误删该语义。

这也意味着：当前事件总线上的**大部分容量是空置的**，隔离/清理问题尚未在多订阅者的复杂场景下暴露，但一旦按设计目标接入更多订阅（可观测性、评估等），串扰与清理缺失会立刻成为阻塞点。

## 4. 影响范围与严重程度

- **影响范围**：所有"多 agent / 多会话共享同一 `EventService` 实例"的用法。单个 agent + 单个会话（各自 `new EventService`）不受影响。
- **严重程度**：中。当前尚未有正式的多 agent 并行共享入口，但一旦共享即发生**静默串扰**，且没有清理手段，排查成本高。
- **现状规避**：测试中通过"每个会话各自新建 `EventService`"规避，见 [test_loop_e2e.py](file:///d:/desktop/软件开发/agent/tests/e2e/test_loop_e2e.py#L114-L123)（注释明确写道"两个会话共享同一条总线会互相把记录写进对方的持久化 buffer"）。

## 5. 当前假设（系统依赖的脆弱前提）

1. **一个 `EventService` 实例只服务一个 agent / 会话**。这是当前正确运行的前提，但**没有任何代码强制**，完全依赖调用方自律（测试里是"每次新建"的约定）。
2. **订阅方不再需要时即被 GC**。只有这样才能靠弱引用自动清理；若调用方长期持有组件引用，注册项将持续有效并继续接收其他 agent 的事件。
3. **同一事件名对应的订阅语义唯一**。目前每个事件名只有一个订阅者，所以"顺序不可控"问题尚未显现；一旦同一事件出现多个不同语义的订阅者，派发顺序与串扰都会成为问题（[service.py](file:///d:/desktop/软件开发/agent/src/myagent/infra/events/service.py#L11-L25) 顶部注释已记录相关的顺序/解绑问题）。

## 6. 触发条件

- 多个 agent / 会话共用同一个 `EventService` 实例；
- 在同一进程内用父组件的 `EventService` 构造新 agent（复用而非新建）；
- 对同一事件名出现第二处、不同语义的注册；
- agent 被 `cancel()` / 结束 / 销毁后，其订阅方对象仍被外部引用而未 GC。

## 7. 临时方案与计划改进

- **临时方案**：让每个 agent / 会话各自持有独立的 `EventService` 实例，禁止跨 agent 复用（当前测试即如此规避）。
- **计划改进**：将来对 `EventService` 进行改造，**引入隔离机制**。方向（尚未定稿，方案落地后另立 technical-debt / plan 文档）：
  - 在订阅与派发链路中引入 agent / session 维度的作用域（scope / owner）键，使 `trigger` 只派发给同作用域的订阅者；
  - 补齐解绑接口（`off` / `unregister`），并把它接入 agent 生命周期，使 agent 结束时可注销自己名下的全部注册。

## 8. 相关文档

- [service.py](file:///d:/desktop/软件开发/agent/src/myagent/infra/events/service.py)（顶部注释：订阅顺序不可控、解绑接口缺失）
- [eventspec.py](file:///d:/desktop/软件开发/agent/src/myagent/infra/events/eventspec.py)（事件登记表，改造时的事件全集）
- [loop.py#L79-L81](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/agent/loop.py#L79-L81)、[persistence.py#L30](file:///d:/desktop/软件开发/agent/src/myagent/agent/core/session/persistence.py#L30)（当前生效的两个订阅节点）
