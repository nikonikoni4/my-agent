# CONTEXT

本项目的领域术语表。由 /grill-with-docs 在术语实际对齐后写入。

消费规则见 docs/agents/domain.md：产出物命名使用本表定义的词汇，不要漂移到近义词。

## 槽位机制

### 槽位（slot）

agent 运行管线上的固定节点。外部行为通过注册挂载其上，节点本身不因扩展而修改。

管线节点固定且少量（turn 开始 → LLM 调用 → 工具调用 → turn 结束），节点上可挂的内容是开放的。

### 注册机制

不修改源码、通过注册新回调来增加行为的扩展方式。本项目的核心价值：新增关注点（权限、超时、脱敏、指标……）= 注册，不改管线。

### 分发语义

一个注册表，两种分发语义（已决策，见 docs/adr/）：

- **emit（扇出）**：只出不进。同一事件发给所有订阅者，不等待、互不影响。观察槽专用。
- **waterfall（链式）**：串行传递。中间件拿到 `next()`：调用 = 放行，不调用 = 否决，包装 = 环绕。拦截槽专用。同构物：Express 中间件、Python ASGI middleware。

### 订阅者（subscriber）

挂在观察槽上的回调。前端渲染、轨迹日志是同一条出口的两个订阅者。

### 中间件（middleware）

挂在拦截槽上、拿到 `next()` 的回调。

## 通道

数据相对 agent 主循环的方向：

- **出口（观察通道）**：内容流出 agent 的单向通道，只出不进。用户可观测（前端）与开发者可观测（轨迹日志）是同一条出口的两个订阅者。
- **机器回流**：程序设定的同步判定（权限门/规则/沙箱），当场放行或拒绝。确定性，不依赖模型自觉。
- **人类回流（问人通道）**：agent 发起、阻塞等待人类回答。场景：权限请求、提问选择。
- **打断（interrupt）**：人类发起、异步到来的反向信号，运行中随时可中止。

## 保证

- **异常包含（containment）**：订阅者/中间件抛异常不影响主流程。订阅者异常被吞掉并记录；中间件异常归一化为否决。
- **fail-closed**：无人应答、通道未装配、异常，一律视为拒绝，绝不默认放行。
- **封闭回答集合**：审批类回流的结果只有 `allowed-once` / `rejected` / `cancelled` / `unavailable`。`unavailable` 为 fail-closed 默认终态。

## 用途

- **轨迹日志（append-only trace）**：事件按序落盘、只追加不修改。审计与评估的素材。
- **回撤**：回到上一节点之前的状态重跑。因工具副作用不可撤销，判定为不可行，不做。

## 避免的近义词

- 不要用"事件总线"与"回调注册表"对立描述本机制——两者是同一物的不同描述层（注册表是内部实现，总线是它扮演的角色）。
- 不要用"回放"指代"回撤"——回放（延续/审计）与回撤（撤销重跑）是不同的概念，可行性完全不同。
- "槽"在 deepseek-harness 前端 UI（SlotMap）另有含义，引用 DHS 材料时注意区分。

## DHS / cordis 对照

| 本项目 | DHS / cordis |
| ---- | ---- |
| 槽位 | 事件名（`interface Events` 声明的点位） |
| 观察槽 / emit | `ctx.emit` / `ctx.parallel` |
| 拦截槽 / waterfall | `ctx.waterfall`（`tools/execute`、`tools/pre-execute`、`llm/stream`、`system-prompt/assemble` 等） |
| 订阅者 | `ctx.on` 注册的 listener |
| 中间件 | waterfall 监听者（拿到 next） |
| 问人通道 | `approval/request` waterfall |
| 打断 | AbortSignal |
