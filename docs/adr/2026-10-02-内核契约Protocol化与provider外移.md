---
version: 1.0
created_at: 2026-10-02
updated_at: 2026-10-02
last_updated: 创建初稿，记录本轮内核 provider 解耦、LLM 异常外移与错误决策输出收口
abstract: 内核删除内部 provider 抽象与实现、改由 Protocol 表达模型调用契约；provider 实现与 LLM 异常分类树整体移入消费方 lifeprismevalue；REQUEST_ERROR 的错误决策输出先收口为现有 waterfall 表达式形状，hitl 改用 ErrorVerdict，tool_use_guard 不在本轮范围。
status: decided
---

# 内核 provider 解耦与错误决策输出收口

## 版本

| 版本 | 更新内容 |
| ---- | -------- |
| 1.0 | 记录本轮改动：内核去 provider 依赖 + provider/LLM 异常外移 lifeprismevalue + 决策输出收口 |

## 问题界定

myagent 要作为独立模块被其他项目导入，就需要内核不强制自带 provider、不解释供应商错误分类、不把重试策略焊死在类型上。本轮确定并落地三件事的形态：

1. 内核内的 provider 抽象与实现如何处置；
2. LLM 异常分类树与重试策略的归属；
3. 错误决策（REQUEST_ERROR 的 waterfall 裁决）输出如何收口。

本轮不设计封闭联合决策类型（草稿 §4 的 `Retry | Continue | Terminate`），也不处理 TOOL_CALL 轴的裁决契约。

## 现状

改动前：

- `core/provider.py` 同时含数据契约类型、`LLMClient` Protocol、`LLMProvider` ABC 三份东西，后者还带 `extract_tool_calls`（OpenAI wire 提取）。
- `llm/openai_provider.py` 继承 `LLMProvider`，内含 SDK 异常 → 领域异常的翻译（`_classify_openai_error` / `_classify_transport_error`）。
- `llm/llm_retry.py` 的 `LLMRerty` 按 `execption.py` 的 LLM 异常类型匹配重试决策。
- `execption.py` 含四个域，其中「LLM 调用路径」域（`LLMCallError` 基类 + 7 个子类）与 SDK 分类强绑定。
- `loop.py:797` 有 `except LLMCallError: raise`。

## 决策前提

1. 内核要能被别的项目当模块复用，而使用方可能已有自己的 provider、异常体系和恢复策略。依据（用户指定文档 `docs/temp/2026-10-02-agent内核与外部策略解耦初稿.md`）：「myagent 需要作为独立模块导入其他项目。使用方可能已有 provider、异常体系和恢复策略；若框架要求使用自身 provider、供应商错误分类或固定重试策略，会造成重复处理和接入耦合。」
2. 内核保留 agent loop 主框架，通过契约接入外部实现。依据（同上文档）：「目标是保留 agent loop 的执行主框架，通过明确的调用契约、事件契约和控制决策契约接入外部实现」。
3. provider、SDK、供应商响应转换归外部实现或可选集成层。依据（同上文档职责边界表）：「provider、SDK、供应商响应转换｜外部实现或可选集成层」。
4. 内核保留自身机制产生的错误（步数上限、熔断）。依据（同上文档）：「框架内完全解耦（除了内部定义的策略错误：最大上限步骤和熔断，以及他们的策略）。」

## 可选方案

| 方案 | 内核与 provider 的关系 | 取舍 |
| ---- | -------------------- | ---- |
| 保留 `LLMProvider` ABC，具体实现仍留内核 `llm/` | 内核随附 provider，仅靠继承解耦 | 未采纳；内核仍携带 SDK 依赖与供应商错误分类，使用方无法换成自己的实现 |
| provider 留内核作「可选集成层」，只去掉继承 | 内核仓保留 `openai_provider`，仅脱离基类 | 未采纳；内核仍带 LLM 异常分类树，`execption.py` 仍在解释供应商错误 |
| provider 与 LLM 异常整体移入消费方 | 内核只留它消费的契约类型，实现归 lifeprismevalue | 当前选择 |

## 决策逻辑

| 前提与决策 | 当前处理 | 前提失效时 |
| ---------- | -------- | ---------- |
| 内核要能被独立复用（前提 1、2） | 内核只留 `LLMClient` Protocol 与数据契约；具体实现由使用方注入 | 若某天只服务单一项目、无复用需求，可重新审议是否随附 provider |
| provider / 异常归属外部（前提 3） | provider、LLM 异常分类树、`LLMRerty` 移入 lifeprismevalue | 若出现第二个消费方，需另定共享位置（如独立包），不在内核合流 |
| 内核保留自身错误（前提 4） | `execption.py` 保留工具执行 / 循环策略 / 兜底三域 | 若新增框架机制错误，仍需在内核建域，不外移 |
| 错误决策输出先固定现状（决策 5） | `ErrorVerdict`（TypedDict）钉住键与取值，仅 loop 边界消费 | 若要封闭联合类型，按草稿 §4 重做，本 ADR 的 TypedDict 是替换点 |

失效条件用于提醒重新审议，不代表用户预先批准切换到某个备选方案。

## 演进历史

| 阶段 | 方案 | 结论 |
| ---- | ---- | ---- |
| 初始目标 | 先删内核 provider，错误处理留到后面 | 用户明确「错误处理先不管，放在后面」 |
| 中间状态 | 内核抽象删除后，provider 具体实现一度出现三份副本（内核、explore 暂存、消费方） | 需要为 provider 定一个正式归宿 |
| 最终确定 | provider、LLM 异常、LLMRerty 整体移入 lifeprismevalue，内核只留契约 | lifeprismevalue 仅新增文件与改 import 行，不改其逻辑 |

## 最终决策

1. **内核删除内部 provider 的抽象与实现；模型调用改由 Protocol 表达契约。**
   - 依据（用户原话）：「这里直接删除内部的provider的内容」
   - 落地范围：`core/provider.py` 删 `LLMProvider` ABC 与 `extract_tool_calls`；`llm/openai_provider.py` 移出内核。`LLMClient` 保留并补齐返回类型（`stream_chat -> AsyncIterator[StreamChunk | LLMResponse]`）。
2. **provider 实现、`LLMRerty` 及与之耦合的错误处理整体移入消费方 lifeprismevalue。**
   - 依据（用户原话）：「现在对llmretry组件进行修改，这个和openaiprovider和其中的一些错误处理是耦合的，把相关的内容都移动到src\lifeprismevalue中」
3. **内核不再持有 LLM 异常分类树：`execption.py` 移除「LLM 调用路径」域（`LLMCallError` 基类 + 7 个子类），保留工具执行、循环策略、兜底三域。**
   - 依据（用户原话）：同上一条「其中的一些错误处理是耦合的……把相关的内容都移动到src\lifeprismevalue中」
4. **迁移不改变 lifeprismevalue 的既有内容。**
   - 依据（用户原话）：「注意这次解耦改动完全不改变src\lifeprismevalue中的内容」
   - 演进：用户随后要求把 provider 与异常移入 lifeprismevalue，该约束据此放宽为「只新增文件与改 import 行，不改其逻辑与提示词」，不再按「一个字符都不动」执行。
5. **REQUEST_ERROR 的错误决策输出先收口为现有 waterfall 表达式的输出，本轮不改造成封闭联合类型。**
   - 依据（用户原话）：「目前就先只固定现有的策略输出为 verdict = await self._event_service.waterfall(REQUEST_ERROR.name,RequestErrorPayLoad(error_type=step_error)) or {} 的输出」
6. **hitl 的裁决输出改用固定类型 `ErrorVerdict`。**
   - 依据（用户原话）：「把他们的输出改为使用ErrorVerdict」；随后限定范围：「就先修改hitl」
7. **tool_use_guard 的输出本轮不改为 `ErrorVerdict`。**
   - 依据（用户原话）：「你是对的tool_use_guard不是这个，这个先不管」
8. **错误处理的时机排在 provider 解耦之后。**
   - 依据（用户原话）：「错误处理先不管，放在后面」

## 决策原因

实例化的 provider 与供应商异常分类属于「外部实现」，性质上归使用方；把它们留在内核，会让内核随附一份 SDK 依赖和一套供应商语义，与「内核可被独立复用」的前提直接冲突（前提 1、3）。把抽象降为 Protocol 后，实现方无需继承内核对基类，只要结构上满足 `LLMClient` 即可注入——这正是决策 1 中 `OpenAIProvider` 改为不继承的效果。

内核错误域保留（前提 4），因为步数上限、熔断是内核自己按配置判定的，判据在内核、可枚举，不依赖任何供应商信息。

决策输出先钉现状（决策 5），是因为草稿 §4 的封闭联合类型会改变产出方的返回值形状与语义（恢复点、终止范围都未定），而本轮目的只是让 loop 边界不再面对裸 dict；`ErrorVerdict` 作为 TypedDict 是这一步骤的落点，也是将来换成封闭联合的替换点。

## 后续影响

### 目标结构

```text
myagent 内核
    ├── core/provider.py    LLMClient(Protocol) + Message/LLMResponse/StreamChunk/... 契约类型
    ├── core/agent/types.py AgentConfig / FinalResult / ErrorVerdict
    ├── execption.py        工具执行域 / 循环策略域 / 兜底域（无 LLM 域）
    └── loop / session / hitl / guard ...

lifeprismevalue 消费方
    └── llm/
          ├── openai_provider.py  OpenAIProvider（不继承，结构化满足 LLMClient）
          ├── exceptions.py       LLMCallError + 7 子类（继承 myagent.infra.exception.MyAgentError）
          └── llm_retry.py        LLMRerty
```

### 已接受的实现选择（非用户决策）

以下为本轮落地时实现方自行做出的选择，用户未表态，不作为用户决策引用：

- `LLMClient` 契约保留非流式 `chat` 方法（loop 实际只消费 `stream_chat`）。
- 删除无任何引用的 `core/agent/deprecated_loop.py` 与 `core/agent/loopcopy.py`。
- `ErrorVerdict.decision` 用 `Literal["retry","backoff_retry","continue","break"]` 钉住取值。
- 迁出的 LLM 异常继续继承 `myagent.infra.exception.MyAgentError`，以保留 `code` / `details` / `to_dict`（`retry_delay` 需读 `details["retry_after"]`）。
- `loop._ask_model` 中的 `except LLMCallError: raise` 判定为纯 re-raise 空操作后移除，行为等价。
- 内核侧 4 个测试文件改为 import `lifeprismevalue.llm.*`。

### 注意事项与实现边界

- `ErrorVerdict` 是 `TypedDict`，**运行时不产生校验**，本轮是纯静态收口，行为零变化。
- 内核测试现依赖 `lifeprismevalue.llm.*`，内核套件反向依赖消费方属架构异味；建议后续把 `test_openai_provider*.py` 搬入消费方测试树，内核只留契约测试。
- `src/myagent/agent/llm/` 迁移后仅剩 `__pycache__`，目录需人工删除。
- 产出方（`llm_retry`、hitl 以外的测试替身）仍返回裸 dict；`ErrorVerdict` 只约束了 loop 边界与 hitl。
- `loop.py` 内仍有两处注释提及 `LLMConnectionError` / `LLMRerty`（说明性文字，无 import 依赖）。
- 本轮未改 `docs/specs` 与 `docs/flows`；相关 flow 文档中指向 `llm/openai_provider.py` 的路径导航需后续同步。
