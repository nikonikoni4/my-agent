---
version: 1.0
created_at: 2026-10-02
updated_at: 2026-10-02
last_updated: 创建初稿：会话还原把 tool_calls 的 arguments 从 dict 归一成转义字符串
abstract: 会话落盘再还原后 Message.tool_calls[].arguments 从 dict 变成 json.dumps 出来的字符串（中文被 ensure_ascii 转义成 \uXXXX），"还原 == 内存"的不变量断裂；写法与读法对同一字段的形态假设相反，且 json.dumps 默认转义与本项目 raw_arguments 明确要求的 ensure_ascii=False 相左。
---

# 还原工具调用 arguments 形态丢失

## 版本

| 版本 | 更新内容 |
| ---- | -------- |
| 1.0 | 创建初稿：还原工具调用 arguments 形态丢失 |

## Bug简述

会话落盘再 `SessionStore.load` 还原后，`Message.tool_calls[].arguments` 由内存里的 **dict** 变成 **JSON 字符串**，且中文参数被转义。以 `{"city": "北京"}` 为例：内存是 `{'city': '北京'}`，还原后是 `'{"city": "\\u5317\\u4eac"}'`。

运行时无任何报错——下游（`ToolRegister.parse_call`、`openai_provider`）两种形态都能吃，所以不影响对话跑通。坏的只有"还原后消息面与内存逐字一致"这一不变量，只在端到端逐条比对时才暴露。

## 复用场景

- **序列化往返已按内存形态原样落盘时，读回侧不要再做"归一化"**。本 bug 的两个端点各自都自洽：写侧原样存，读侧按自己的假设归一，合起来就丢形态。
- **字段声明为 `X | Y` 两形态、靠 `isinstance` 分流时，写侧与读侧必须对"哪种形态是权威"达成一致**。任一侧擅自归一，`内存 == 还原` 这个等式就断。同类风险点：`ToolCallData.arguments`、`Message.content`（str / 块列表）。
- **`json.dumps` 的 `ensure_ascii` 默认是 `True`**。凡是要把中文参数序列化成对外文本的地方，都要显式传 `ensure_ascii=False`——本项目 `RawToolCall.raw_arguments` 的 docstring 已明确写过这条理由（默认转义会让中文参数体积与 token 翻几倍），读回侧这行漏了。

## 代码位置

- `src/myagent/agent/core/session/store.py:136` —— `SessionStore._restore_message()` 里的归一化语句：

  ```python
  arguments=tc["arguments"] if isinstance(tc["arguments"], str) else json.dumps(tc["arguments"] or {}),
  ```

- `src/myagent/agent/core/session/types.py:25` —— `SessionData.to_record_dict()` 走 `asdict()`，写侧**不归一化**，按内存形态原样落盘
- `src/myagent/agent/core/session/types.py:132-136` —— `ToolCallData.arguments` 注释已写明"落盘按拿到时的形态原样存……不在这里归一化"，是同一条策略的书面记录
- `src/myagent/agent/core/provider.py:34` —— `RawToolCall.arguments` 的两形态契约（解析成功为 dict，否则 wire 原文串）
- `src/myagent/agent/core/provider.py:71-80` —— `raw_arguments`，明确 `ensure_ascii=False` 及其理由
- 暴露用例：`tests/e2e/test_loop_e2e.py` 两个用例的 `还原后消息逐条内容一致` 断言

## 发生原因

写侧与读侧对同一字段的形态假设相反。

**写侧原样存。** `SessionData.to_record_dict()` 用 `dataclasses.asdict()` 递归展开，`RawToolCall` 是 dataclass，其 `arguments` 按内存形态原样落盘。实测（构造一个解析成功 + 一个解析失败）：

```
c1 dict {'city': '北京'} truncated=False
c2 str  '{oops'          truncated=False
```

**读侧单方面归一。** `_restore_message` 的注释按"现契约为 wire 原样字符串"行事，把非 str 的 `arguments` 一律 `json.dumps` 成字符串。于是 dict 在还原时被转成文本，且因默认 `ensure_ascii=True` 而中文转义。

两者各自自洽，冲突只在中间。附带损失：`truncated` 也没还原（`asdict` 存了它，构造时却只传了 id/name/arguments），还原后的调用会丢掉截断标记。

## 最佳方案

读回侧去掉归一化，按拿到时的形态原样还原，并把 `truncated` 一并带回来：

```python
tool_calls = None if raw_tool_calls is None else [
    RawToolCall(
        id=tc["id"],
        name=tc["name"],
        # asdict 已按内存形态落盘（解析成功为 dict、未解析为 wire 原文 str），
        # 这里原样还原，与 RawToolCall.arguments 的两形态契约一致。
        # 不要再 json.dumps 归一化：那会把 dict 变成转义字符串，破坏"还原 == 内存"。
        arguments=tc["arguments"],
        truncated=tc.get("truncated", False),
    )
    for tc in raw_tool_calls
]
```

**不要改写侧**。把落盘统一成 wire 字符串虽然也能让等式成立，但那等于为迁就读侧的错误假设去改数据格式，且与 `ToolCallData.arguments` 已写明的"原样存"策略冲突。

## 遗留注意

1. **改前先确认没有读回侧依赖"还原后 arguments 一定是字符串"**。目前核到两处两种形态都能吃——`tool/register.py:258`（非 str 直接当作已解析）、`llm/openai_provider.py:335`（dict 直接用，否则 `json.loads`）。这不等于已穷尽消费方，落地前应再扫一遍 `arguments` 的读取点。
2. **`truncated` 的还原属附带修复，别与主问题混谈**。它与本 bug 同源（同一个构造点漏还原），但 `RawToolCall.to_dict()` 有意不序列化 `truncated`（它是执行语境而非 wire 数据，见 `provider.py:28-30`）——那是**发给 LLM 的路径**不存它，与会话落盘的 `asdict` 存它是两回事，不要据此认为落盘也不该有。
3. **验证方式**：修完 `tests/e2e/test_loop_e2e.py` 的两个用例应越过 `还原后消息逐条内容一致` 这一断言，继续校验后续排查点。这两个用例走真实 ARK API（凭证在项目根 `.env`），缺凭证会自动 skip。
4. **本 bug 的暴露与被发现是两回事**：它是既有缺陷，长期被该 e2e 用例更前置的构造错误（`AgentConfig` 缺 `max_retry_count`）挡住，整个文件跑不到断言。补齐构造参数后才第一次跑到这一层。同类隐藏方式值得留意——**前置报错会让后面的断言形同虚设**。
