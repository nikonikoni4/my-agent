---
version: 1.0
created_at: 2026-09-10
updated_at: 2026-09-10
last_updated: 创建文档初稿；确定 LLM 调用失败时在 `_ask_model` 内补齐 finish 块（finish_reason=error）并原样抛出 LLMCallError
abstract: LLM 调用失败（LLMCallError，不含取消）时 session 的收尾策略：已产出的 chunk 不回滚（append-only）、缺少 finish 块则补一条 finish_reason="error" 的 finish 块、不补 assistant/message，随后原样抛出异常；补齐发生在 `_ask_model` 内部（唯一知道流已产出什么的地方）
status: decided
---

# LLM 调用失败时的 session 补齐策略

## 版本

| 版本 | 更新内容 |
| ---- | -------- |
| 1.0 | 创建文档初稿 |

## 问题界定

### 问题简述

`_ask_model` 消费 `stream_chat` 时，模型流可能中途抛 `LLMCallError`（SDK 异常经 llm 层翻译）。此时 session 里已经落了一批 `assistant/chunk`，但没有 `assistant/message`（流没走完，没有完整回复可落盘），也没有 finish 块（provider 只在流正常结束时产出）。需要决定：**失败发生后，session 该补什么、补在哪一层、已产出的内容怎么处理**，以便日志能清晰区分"正常结束"与"调用失败"，且不破坏 append-only 契约。

### 讨论范围

- 失败时是否回滚已产出的 chunk。
- 失败时是否补 finish 块，以及 `finish_reason` 取什么值。
- 失败时是否补 `assistant/message`。
- 补齐动作的归属层（`_ask_model` 内 vs `step` 的 `except`）。

### 非讨论范围

- **取消（`asyncio.CancelledError`）的收尾**：取消单独处理、不走 request/error，本策略不覆盖（见后续影响）。
- **`request/error` waterfall 的接线与重试动作**：独立在途决策；本 ADR 只定"失败现场补什么"，不定"失败之后重试还是放弃"。
- **补进去的 finish 块由谁消费**（可观测/trace/UI）：当前无消费方，属后续工作。
- **`finish_reason` 是否在 `assistant/message` 上再存一份**（双写形态）：本 ADR 只定 finish 块的补齐，不定 message 侧冗余。

### 模糊信息的明确定义

- **已产出 chunk**：本次 `_ask_model` 中，出错前已经从 `stream_chat` yield 出来并 append 进 session 的 `assistant/chunk`（content / reasoning / tool-call / finish 四类）。
- **正常结束**：`stream_chat` 走完，尾部产出 finish 块（`StreamChunk.finish_reason` 非空）并随后产出 `LLMResponse`。
- **调用失败**：`stream_chat` 中途抛 `LLMCallError`，无 `LLMResponse`。
- **补齐**：在失败现场向 session 追加记录，使日志对"为什么这次调用没有正常的收尾"有显式答案。

### 问题深度

涉及"错误现场的信息重建"与"append-only 日志的完整性"两个原则的交汇：编排层要在不改写历史的前提下，让"半截流"在日志上具备可判别的收尾形态。这是长期维护方式决策（错误收尾约定），不是单点实现细节。

## 现状（决策时）

- `openai_provider.stream_chat` 在流尾产出 finish 块：`if finish_reason is not None: yield StreamChunk(finish_reason=finish_reason)`；该行只在 `async for` 正常走完后执行，中途抛 `LLMCallError` 则自然不产出——**"finish 块缺失"本身可作异常信号**。
- `StreamChunk` 已带 `finish_reason` 字段；`AssistantChunkData` 以独立 `finish_reason` 字段承载 finish 块（不再借用 `texts`），落盘为 `text-chunk` 的 `finish_reason` 列表（见 chunk 体系改造）。
- `_ask_model` 此前只做透传：chunk 逐个 append `assistant/chunk`，`LLMResponse` 到达时 append `assistant/message`；无 `try/except`，`LLMCallError` 直接冒泡到 `step`。
- `step` 已捕获 `LLMCallError` → 记 `step_error` → `finally` 触发 `REQUEST_ERROR`（waterfall，当前无订阅方则抛 `LLmError`）。
- 被取代方向已记录：`2026-09-10-工具调用解析与截断处置移入工具层.md` 把"LLM 调用错误（不含取消）处理中补 finish 块"列为后续任务。

## 决策前提

- 前提 1（append-only）：session 是 append-only 日志，已落盘的内容不再被改写；持久化层也就"整批原子性"承诺过两级回滚（见 `2026-09-04-session持久化写入两级回滚.md`），失败现场不具备"删掉已产出 chunk"的语义。
- 前提 2（失败无完整回复）：stream 中断时没有完整 `LLMResponse`，tool_calls 可能只拼了一半，content 也可能被截断。
- 前提 3（可判别性）：日志消费者需要能区分"正常结束"与"调用失败"；仅靠"缺 finish 块"是隐式约定，对 trace/可观测不够友好。
- 前提 4（分层）：`_ask_model` 是唯一知道"流已经产出过什么、是否已产出 finish 块"的地方；`step` 只看得到异常。
- 前提 5（补齐不等于兜底）：补齐只负责让现场自洽，不负责决定后续动作（重试/放弃由 request/error 侧决定）。

## 可选方案

### 方案 A：保留已产出 chunk + 补 error finish 块 + 不补 assistant/message（选定）

在 `_ask_model` 内包 `try/except LLMCallError`：已产出 chunk 不动；若本次尚未产出 finish 块，补一条 `AssistantChunkData(StreamChunk(finish_reason="error"))` 并触发 `ASSISTANT_CHUNK`；不补 `assistant/message`；随后原样 `raise`。

**优势**

- 日志自洽：每条"开始过一次模型调用"的记录都以 finish 块收尾——正常结束是真实 `finish_reason`，失败是 `"error"`，判别不需要理解隐式约定。
- 与 append-only 一致：不改写、不回滚任何已落盘内容。
- 与分层一致：补齐在唯一掌握流状态的地方完成，`step` 不需要额外传递"流进度"。
- 异常语义不变：原样 `raise`，`step` 的 `except LLMCallError` 与后续 request/error 接线不受影响。

**劣势**

- `"error"` 是新增的 `finish_reason` 取值，需要在 finish 块的消费方（未来）里认识它。
- 同一个 `step` 若因 request/error 决策发生重试，会再走一次 `_ask_model`，日志里会出现多条 finish 块（每轮一条），需要消费方按 step/轮次归组理解。

### 方案 B：什么都不补，靠"缺少 finish 块"判别（否决）

**优势**：实现为零，provider 已经天然不产出。

**劣势**

- 隐式约定：消费方必须知道"正常一定有 finish 块、没有就是失败"，且无法区分"失败"与"provider 未实现/旧格式/其他原因导致的缺失"。
- 可观测性差：trace 上这一段流看起来只是"戛然而止"，没有显式的错误收尾记录。

### 方案 C：补一条 interrupted 的 assistant/message 收口（否决）

把已流出的 content / reasoning 前缀固化成 `assistant/message` 并标记中断。

**优势**：模型可见历史上有一段"被打断的助手消息"，语义接近 dsh 对"实时取消"的处理。

**劣势**

- 与本项目当前的分工不符：dsh 的 `assistant/message.interrupted` 用于**实时取消**（turn 被 cancel），而这里是**调用失败**；两者后续处置不同（取消不回喂，失败可能要重试）。
- 前提 2：流中断时 tool_calls 可能只拼了一半，固化进 `assistant/message` 会把不完整的调用请求带到模型可见历史。
- 越权：决定"半截内容是否进入模型可见历史"属于重试/降级策略，应由 request/error 侧在拿到现场后决定，不在补齐动作里预先拍板。

### 方案 D：回滚已产出的 chunk（否决）

**优势**：失败后日志看起来"干净"，如同这次调用没发生过。

**劣势**

- 直接违反前提 1：与 append-only 及持久化的原子性契约正面冲突；已落盘的 chunk 无法安全撤销。
- 丢失现场：出错前的推理/正文前缀恰恰是排查最有价值的部分。

## 决策逻辑

| 前提条件 | 对应方案 | 备注 |
|----------|----------|------|
| 前提 1 + 2 + 3 + 4 + 5 成立（当前状态） | 方案 A | 当前选择 |
| 补出来的 finish 块始终无人消费，且"缺块即失败"被所有消费方显式约定 | 可退回方案 B | 备选触发 |
| 后续决定"半截前缀必须进模型可见历史"（例如失败也要让模型看见自己的半截输出） | 在 request/error 侧增加固化为 interrupted `assistant/message` 的动作 | 备选触发 |
| 持久化契约改为可变日志（允许撤销） | 重新评估方案 D | 防御性调整 |

## 演进历史

| 版本 | 方案 | 解决的问题 | 引入的新问题 |
| ---- | ---- | ---------- | ------------ |
| v1（2026-09-10） | 保留 chunk + 补 error finish 块 + 不补 message，在 `_ask_model` 内补齐 | 让"调用失败"在 append-only 日志上有显式收尾，且与正常结束可判别 | 新增 `"error"` 取值需消费方认识；重试多轮会留下多条 finish 块 |

## 最终决策

当前成立的前提：前提 1—5。

因此选择**方案 A**：LLM 调用失败（`LLMCallError`，不含取消）时，在 `_ask_model` 内补齐 session——已产出 chunk 保持在原位不回滚；本次未产出 finish 块则补一条 `finish_reason="error"` 的 finish 块并触发 `ASSISTANT_CHUNK`；不补 `assistant/message`；随后原样抛出 `LLMCallError`。

前提失效时的切换路径：见决策逻辑表。

## 决策原因

- 原因 1（对应前提 1）：append-only 是 session 与持久化的共同契约，失败现场不具备"撤销"语义，因此只能"追加收尾"，不能"删改历史"。
- 原因 2（对应前提 2）：没有完整 `LLMResponse`，tool_calls 与 content 都可能不完整，补 `assistant/message` 会把半成品伪装成完整回复，污染模型可见历史。
- 原因 3（对应前提 3 + 前提 4）：finish 块的语义就是"这次流怎么结束的"，失败也是"结束原因"的一种；由掌握流状态的 `_ask_model` 补写，判别是显式的，而不是靠"缺失"推断。
- 原因 4（对应前提 5）：补齐只让现场自洽，不预设后续动作；是否重试、是否降级留给 request/error 侧基于完整现场决策。

## 后续影响

- **代码结构**：`_ask_model` 持有 `finish_emitted` 标记（`item.finish_reason is not None` 时置位），`try/except LLMCallError` 仅补缺并 `raise`；`TimeoutError` / `CancelledError` 不被此处截获，仍走 `step` 原路径。
- **数据**：finish 块以独立 `finish_reason` 字段落盘（`text-chunk` 行），失败时为 `["error"]`；`finish_reason` 当前**只**存在于 finish 块，未在 `assistant/message` 上冗余（双写形态未定）。
- **可判别**：日志上"正常结束（stop/length/tool_calls…）"与"调用失败（error）"均由 finish 块显式表达。
- **已知限制**：取消路径（`CancelledError`）不补 finish 块；`step` 因 request/error 决策重试时，每轮各补一条 finish 块，消费方需按 step/轮次归组；补出的 finish 块当前无消费方。
- **后续任务**：`request/error` waterfall 接线与重试动作（独立在途决策）；取消场景的收尾策略；finish 块消费方（trace / 可观测）接入；`finish_reason` 是否在 `assistant/message` 侧双写。
- **测试**：假 provider 在流中途抛 `LLMCallError` → 断言已产出 chunk 保留、补一条 `finish_reason="error"` 的 finish 块、无 `assistant/message`、异常继续上抛；已产出 finish 块后再失败时不重复补。
