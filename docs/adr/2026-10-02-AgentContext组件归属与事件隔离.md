---
version: 1.1
created_at: 2026-10-02
updated_at: 2026-10-02
last_updated: 增补 Store 无总线依赖、Session 显式绑定时启用事件与持久化；重复绑定改为抛 SessionReBindError
abstract: AgentContext 持有完整组件与独立总线；Store 仅加载数据，Session 首次显式绑定才启用事件和持久化，重复绑定抛 SessionReBindError 并保留原绑定。
status: decided
---

# AgentContext 组件归属与事件隔离

## 版本

| 版本 | 更新内容 |
| ---- | -------- |
| 1.0 | 根据用户交接摘要和本次架构讨论记录决策，区分目标架构与尚未完成的实现 |
| 1.1 | Store 不再持有总线；Session 构造不启动任务，首次 bind 才接入事件并创建持久化；重复绑定抛 SessionReBindError 并拒绝 |

## 问题界定

多个 agent 共用只按事件名分组的 EventService 时，同名事件会触达其他 agent 的订阅者。需要确定隔离落点，并明确 AgentContext 的语义、组件依赖和生命周期。

本次覆盖组件归属、事件隔离、装配和关闭顺序；不设计部署全局服务层、跨 agent 通信协议、动态服务注册框架，也不处理交接摘要中四个既有测试失败。

### 语义定义

| 对象 | 语义与职责 |
| ---- | ---------- |
| AgentContext | 一个完整的 agent 实例的持有者；一个 ctx 只服务于一个 agent，持有其所需组件并统一表达归属和生命周期 |
| EventService | 一个 agent 内部的事件总线；负责订阅、同步通知 emit 和异步裁决 waterfall，不承担 agent 路由 |
| ReActAgentLoop | agent 的执行循环；使用直接依赖，管理执行过程和运行状态 |
| Session | 可独立访问的会话数据；显式绑定运行依赖后，自持持久化组件并广播 session/event |
| SessionPresist | Session 内部的持久化组件；负责记录缓冲、文件写入和自身后台任务 |
| SessionStore | 无 ctx 或 EventService 依赖的会话创建与加载入口；返回未绑定的 Session，不启动持久化 |
| SystemPrompt | 当前 agent 的提示词组件；内部不再以 agent_name 区分不同 agent |
| 策略组件 | 重试、熔断、人在回路等事件订阅方；通过总线参与通知或裁决，由 ctx 强引用持有 |

“持有”表示组件归属与强引用保活；“依赖”表示组件执行职责时需要使用另一个对象。两者不要求由同一对象承担。

## 现状

用户提供的《myagent 事件隔离改造——交接摘要》说明：原共享总线没有 agent 路由；已开始通过独立 ctx 持有总线解决隔离，Session 已改为直连持久化，但 loop 已改成接收 ctx，ctx 尚仅持有 name 和总线。

本次讨论修正了依赖方向：独立总线已足以隔离事件，组件不必为隔离而接收 ctx。原有 ctx 透传方案与本文目标架构存在差异，后续需调整；本文不表示实现已经完成。

后续用户指出 Store 持有总线会把纯数据访问与 agent 运行绑定，要求先修改 Store 和 Session。v1.1 已将数据构造与运行绑定分离，并迁移必要调用点。完整 ctx/loop 重构不在本轮实现范围：调用点暂沿用现有 ctx 的事件透传接口，最终仍按目标架构注入实际 EventService。

生命周期讨论指出，loop 和 Session 的持久化组件均有后台任务。活动任务可以继续保留协程及相关对象；丢弃调用方对 ctx 的引用不能代替任务停止和缓冲落盘。这里记录运行约束，不宣称已验证内存泄漏或数据丢失。

## 决策前提

1. 一个 ctx 只服务于一个 agent。用户原话：「这里假设一个ctx只服务于一个agent。」
2. ctx 表达完整组件集合。用户原话：「我原来提出的把循环也放进context，这样相当于一个agent context即一个agent所需的所有内容。」
3. 组件可以保留 EventService 直接依赖。用户原话：「当前似乎并不需要把ctx传入各个组件，仍和以前一样只传入eventservice。」
4. 普通组件的主要强引用归属于 ctx 和 loop。用户原话：「sesion只被loop和ctx持有」「systemprompt 也只被ctx和loop持有」「策略也是只被ctx持有」。这是目标装配约束，不保证外部调用方或活动任务不存在其他引用。
5. 纯会话数据访问不需要启动运行依赖。用户原话：「若我本身只需要session以及里面的数据，不需要eventservice，这里可以直接不启动」。
6. 绑定是一次性操作。用户原话：「session改为显示绑定，而不是初始化的时候绑定，session的持久化我觉得也应该在显式绑定的时候进行创建，并且这里增加一个不幂等操作，已有的session不可重复绑定，发出警告」。本文将“显示绑定”按上下文解释为“显式绑定”。落地时用户将告警收紧为异常：重复绑定抛 SessionReBindError（见最终决策 8）。

用户在生命周期讨论后明确：「大体内容我确定了，现在你直接输出一个架构说明，先定义语义，然后说明架构以及注意事项」，并在架构说明后要求：「你直接写adr吧，需要包含上述内容」。本文据此记录上述架构及关闭约束；不追加未讨论的功能。

## 可选方案

| 方案 | 隔离与组件访问方式 | 取舍 |
| ---- | ---------------- | ---- |
| 共享总线增加 agent 路由 | 订阅和派发携带 agent 身份 | 可在一个总线上路由多个 agent，但需贯穿身份参数；当前一 ctx 一 agent 前提下不需要 |
| ctx 仅持有总线并透传 | 组件接收 ctx，通过 ctx.register/emit/waterfall 使用总线 | 能隔离，但把只需要总线的组件耦合到上下文；无法表达用户所需完整组件集合 |
| 完整 ctx + 直接依赖注入 | ctx 持有组件和独立总线；组件接收各自依赖 | 当前选择；组件不反向依赖 ctx，归属明确，隔离由总线实例保证 |
| ctx 按名查表或 TYPE_CHECKING 后挂 | 为组件互相解析或双向引用提供间接访问 | 当前没有该需求；直接依赖方向下没有需要用它们解决的导入环 |
| Store 持有总线，Session 构造即启动持久化 | create/load 同时接入运行依赖 | 不采用；纯数据加载也被迫需要总线与运行中的事件循环 |
| Store 仅加载数据，Session 显式绑定 | create/load 返回未绑定对象，bind 时启用事件与持久化 | 当前选择；运行装配方负责执行前绑定 |

## 决策逻辑

| 前提与决策 | 当前处理 | 前提失效时 |
| ---------- | -------- | ---------- |
| 一个 ctx 只服务一个 agent（决策 1） | 每个 ctx 独立持有总线 | 若需要一个 ctx 承载多个 agent，重新审议作用域；不自动采用共享路由 |
| ctx 是完整组件持有者（决策 2） | 强引用持有 loop、Session、SystemPrompt 和策略 | 若需要共享或迁移组件，重新定义归属和替换责任 |
| 组件只需直接依赖（决策 3） | 注入 EventService 等对象，避免反向导入 ctx | 若出现真实动态服务解析需求，再审议访问契约 |
| agent 含活动后台任务（决策 4） | 先停止执行和持久化，再释放 ctx | 新增后台任务或外部资源时，纳入收尾；仅普通对象时自然回收 |
| 数据可独立访问（决策 5、6） | Store 无总线，Session 构造不绑定 | 若要求加载即运行，重新审议显式启动契约 |
| 持久化属于运行绑定（决策 7） | 首次 bind 才创建持久化组件 | 若需要独立写入模式，另行定义接口，不暗中启动任务 |
| Session 不可重复绑定（决策 8） | 同一或不同总线的重复绑定均抛 SessionReBindError 并拒绝 | 若需要迁移归属，重新审议迁移与收尾协议，不覆盖原绑定 |

这些失效条件用于提醒重新审议，不代表用户预先批准自动切换到某一备选架构。

## 演进历史

| 阶段 | 方案 | 本次讨论结论 |
| ---- | ---- | ------------ |
| 初始考虑 | EventService 增加 agent 路由层 | 用户提出通过 ctx 分别持有总线 |
| 交接时方案 | ctx 私有总线，loop 接收 ctx 并使用透传 | 隔离成立，但引出 ctx 与 loop 双向依赖及组件挂载问题 |
| 本次确定 | ctx 持有完整组件，组件接收直接依赖 | 隔离不要求 ctx 透传；单向依赖消除此前导入环前提；补充后台任务关闭约束 |
| v1.1 后续确定 | Store 数据加载与 Session 运行绑定分离 | bind 同时接入总线与创建持久化；重复绑定抛 SessionReBindError，不作为正常重复装配 |

## 最终决策

1. **一个 AgentContext 只服务于一个 agent，独立持有该 agent 的 EventService；不在总线内部增加 agent 路由层。**
   - 依据（用户原话）：「原来的设计是eventservice 增加一层agent路由事件，后面我提出不如直接增加ctx。这里假设一个ctx只服务于一个agent。」
2. **AgentContext 持有一个 agent 的完整组件集合，包括 loop、Session、SystemPrompt 和策略；Session 内部持有持久化组件。**
   - 依据（用户原话）：「我原来提出的把循环也放进context，这样相当于一个agent context即一个agent所需的所有内容。」
   - 依据（用户原话）：「sesion只被loop和ctx持有」「systemprompt 也只被ctx和loop持有」「策略也是只被ctx持有」。
   - Session 内部持久化归属沿用用户提供的交接摘要；用户要求本文「需要包含上述内容」。
3. **各组件继续接收 EventService 等直接依赖，不为事件隔离而接收整个 ctx；无需 ctx 的三个事件透传方法。**
   - 依据（用户原话）：「当前似乎并不需要把ctx传入各个组件，仍和以前一样只传入eventservice。好像本来也没有循环导入的问题。」
   - 该方向随后纳入架构说明，用户明确要求：「你直接写adr吧，需要包含上述内容」。
4. **完整 agent 的关闭先结束 loop，再完成 Session 持久化收尾，最后释放 ctx；关闭编排由 ctx 统一负责。**
   - 依据（用户原话）：「你直接写adr吧，需要包含上述内容」。该指令所指的上一份架构说明明确列出上述关闭顺序和 ctx 统一负责关闭编排。
   - 不把此前用户问句「清理的时候调用loop.cancel使得后台的循环关闭就可以了？」当成“cancel 单独足够”的决策。
   - 统一异步关闭入口的具体名称、签名和关闭异常契约尚未定义；本文不指定 dispose() 或 close()。
5. **SessionStore 不持有 EventService 或 ctx，create/load 仅返回未绑定的 Session。**
   - 依据（用户原话）：「最关键的是store本身是需要持有eventservice的。这里的eventservice能否改为延迟加载」；随后明确要求：「把这个也写进adr，并且这个你先直接修改store和session」。
6. **Session 构造不接入总线，由调用方显式 bind(event_service)。**
   - 依据（用户原话）：「session改为显示绑定，而不是初始化的时候绑定」。
7. **持久化组件在首次显式绑定时创建，构造与加载期间不创建后台写入任务。**
   - 依据（用户原话）：「session的持久化我觉得也应该在显式绑定的时候进行创建」。
   - 沿用内存态会话约定：没有 session_file 时，绑定只启用事件，不创建持久化组件。
8. **已有绑定的 Session 不可重复绑定，包括同一总线；抛出 SessionReBindError 并保留原总线、持久化组件和任务。**
   - 依据（用户原话）：「这里增加一个不幂等操作，已有的session不可重复绑定，发出警告」；落地时用户要求把告警改为异常，本条以异常为准。
   - “不幂等”指重复调用会抛错而非静默复用，不能作为正常重复装配；异常抛出前不替换绑定、不创建额外任务、不重复提交记录，原绑定状态保持可用。

## 决策原因

实例归属已经提供事件作用域，因此无需在订阅和派发链路重复传递 agent 身份，也无需用 ctx 透传包装同一总线。

完整 ctx 满足用户对“一个 agent 所需的所有内容”的定义。ctx 持有组件表示归属，loop 引用组件表示使用，两者可以引用同一个实例，无须建立第二套状态。

依赖注入保持单向：ctx 可以导入具体组件，组件不导入 ctx。此前的循环导入来自 loop 接收 ctx、ctx 又持有 loop 的设计组合，不是完整组件持有者的必然问题。

## 后续影响

### 目标架构

```text
AgentContext
    ├── EventService
    ├── Session
    │     └── SessionPresist
    ├── SystemPrompt
    ├── 策略组件
    └── ReActAgentLoop

组件依赖：
    ReActAgentLoop → EventService、Session、SystemPrompt
    Session       → EventService、SessionPresist
    策略组件       → EventService
```

ctx 与 loop 使用同一个 Session、SystemPrompt 实例。组件继续通过 event_service.register/emit/waterfall 使用总线。ctx 不需要服务名查表、TYPE_CHECKING 后挂或组件反向引用。

Session.append 先把记录提交到自身持久化缓冲，再广播 session/event。没有监听器也能提交持久化记录；提交缓冲不等于记录已落盘。

上述持久化与广播仅在绑定后启用。未绑定时 append 只更新内存记录和消息视图，不广播、不落盘、不启动任务。

### 数据加载与显式绑定接口

```python
store = SessionStore(session_folder)
session = store.load(session_id, project_path)  # 或 store.create(name, project_path)
# 纯数据使用：直接访问记录或 derive_messages，无须总线和事件循环。

# 运行装配：文件态会话在运行中的事件循环内绑定。
session.bind(event_service)
# 此后启动或调用 loop；loop 不隐式绑定 Session。
```

Session 构造签名为 Session(meta_data, record_list=None, session_file=None)，仅保存数据和路径。bind(event_service) 首次接入总线；存在路径时创建 SessionPresist。重复调用抛 SessionReBindError，且在抛出前不替换绑定、不创建额外任务、不重复提交记录。

### 已接受的实现选择（非新增用户决策）

- 绑定前追加的记录保留在内存，首次绑定时将新增记录快照提交到持久化缓冲，避免绑定后写出的会话缺少这段记录。
- 构造时已有记录按加载历史处理，不重复写入；绑定动作不补发历史或未绑定期间的事件。
- 重复绑定以 SessionReBindError 上报，异常消息含 session_id 与“重复绑定”，便于装配方直接定位是哪个会话被重复装配。

这些是本轮实现语义，不扩展为历史事件回放或独立持久化模式。

SystemPrompt 去掉 agent_name 维度，沿用交接摘要中已确认的简化方向；该项不表示目前已经实现。

### 生命周期与关闭顺序

1. ctx 调用 loop.cancel()，请求停止执行。
2. 等待 loop 后台任务结束，使中断记录和执行收尾完成。
3. 完成 Session 剩余缓冲的持久化，停止并等待持久化后台任务结束。
4. 释放 ctx；没有其他强引用的普通组件自然回收。

先结束 loop，再结束持久化，因为执行收尾期间仍可能追加会话记录。cancel() 仅表示发出取消请求，等待任务完成才表示运行已经结束。

活动 Task 可以通过协程的 self 保留 loop，进而保留其依赖；持久化 Task 也可以保留 SessionPresist。不能以“调用方不再引用 ctx”推断整个组件集合已销毁。关闭期间保留 ctx，避免弱引用策略先失去强引用而执行任务仍活着。

普通 SystemPrompt、策略等无活动任务或外部资源时不要求销毁接口。EventService 不因本次隔离增加 off；若将来需要组件仍存活时主动停止订阅，需另行讨论。

### 注意事项与实现边界

- 同一 agent 的生产方和订阅方必须使用同一份 EventService；不同 agent 不共用总线。隔离依赖装配契约。
- 独立总线不自动隔离共享可变状态。Session、SystemPrompt 和有状态策略按 agent 归属；外部保留组件引用时，回收时间相应延后。
- 总线弱引用不负责保活订阅者；策略及临时闭包必须有持续有效的强引用所有者。
- 对外暴露哪些 ctx 组件由具体接口需求决定；持有完整集合不要求公开全部内部字段。
- 没有部署全局层和 unscoped 监听器；跨 agent 观测由装配方显式聚合，这是交接摘要已明确的功能取舍。
- 一份活动 Session 的持久化写入归属于自身；本文不新增多写入者并发协议。
- 实现需调整现有 loop 接收 ctx 的方向、完善 ctx 组件装配与异步收尾，并完成既定 SystemPrompt 简化；ADR 本身不执行这些代码修改。
- v1.1 的 Store/Session 分离已实现；其余完整 ctx/loop 重构仍待后续处理。文件态 Session.bind 需要运行中的事件循环，create/load 不需要。
- 后续验收应验证同名事件跨 agent 不串扰、策略在运行期间持续有效、loop 收尾记录先于持久化任务结束、剩余缓冲完成写入、关闭后任务不再运行。具体关闭异常处理契约在实现前补齐。
