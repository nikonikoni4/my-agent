# myagent

一个用于学习的 ReAct Agent 框架，落在 `src/myagent/`。三个目标：

- **可观测**：agent 的每一步、每次工具调用、每次错误处置都落成 session 记录，事后能定位到是哪一步出的问题
- **可扩展**：事件、工具、错误策略都通过注册接入，加新能力不改核心
- **能稳定运行**：有重试退避、错误处置账本、功能降级、人在回路

参考实现：`D:\desktop\软件开发\deepseek-harness`、`D:\desktop\软件开发\nanobot`。

## 快速开始

```bash
pip install -e .          # requires-python >= 3.12
```

项目根 `.env` 提供模型凭证（`config/system_config.py` 在导入时读一次）：

```
MODEL="doubao-seed-2.0-lite"
BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ARK_API_KEY=...
```

myagent 本身没有 CLI 入口，跑起来靠消费方。当前唯一的可运行主程序在 `lifeprismevalue`：

```bash
python -m lifeprismevalue.agent.console
```

控制台支持：普通文本走 `agent.followup()`、`/cancel` 打断当前轮、`/exit` 退出；人在回路提问挂起时，输入行被当作选项应答。

测试：

```bash
pytest                       # 全量
pytest -m "not e2e"          # 跳过真实 LLM 调用（e2e 慢且烧 token）
pytest tests/e2e -v          # 只跑 e2e；缺 ARK_API_KEY 自动 skip
```

## 目录结构

```
src/myagent/
├── agent/
│   ├── core/                    内核：不绑定具体供应商，LLM 以抽象契约接入
│   │   ├── provider.py          LLM 契约层：Message / RawToolCall / LLMProvider 抽象
│   │   ├── agent/               ReActAgentLoop、AgentConfig、FinalResult
│   │   ├── session/             Session 账本、SessionStore、异步落盘、消息面视图
│   │   ├── tool/                Tool 抽象基类、ToolRegister（校验/执行/熔断）
│   │   └── systemprompt/        系统提示词注册、遮蔽、组装、渲染
│   ├── llm/                     具体供应商：OpenAIProvider、LLMRerty 重试策略表
│   ├── hitl/                    人在回路：错误裁决的 waterfall 订阅方
│   ├── guard/                   ToolUseGuard 路径白名单护栏
│   └── execption.py             异常分类树
├── infra/
│   ├── events/                  EventService：事件登记、订阅、emit / waterfall 派发
│   └── exception.py             MyAgentError 基类
├── config/                      .env 读取
├── evaluate/                    ToolEvaluate 工具调用评估器
└── utils/

src/lifeprismevalue/             基于 myagent 的第一个消费方（不是框架的一部分）
```

## 核心机制

### 事件总线（`infra/events/`）

事件名遵循 `域/动作`，集中在 `eventspec.py` 登记。两种语义：

- **`emit(name, payload)`** —— 纯通知，同步，一个回调出错不影响其余
- **`await waterfall(name, payload)`** —— 异步洋葱链，订阅方签名 `(payload, _next)`，**必须 async**；用于让多方对同一件事依次表态（如工具调用审批、错误裁决）

订阅方的注册顺序即 waterfall 的洋葱层级（先注册者最外层）。**EventService 持弱引用**——lambda / 闭包必须调用方自持引用，否则立即失效。

已登记的事件：`turn/start`、`step/start`、`request/header`、`user/message`、`assistant/chunk`、`assistant/message`、`tool/call`、`tool/result`、`step/end`、`turn/end`、`session/event`，以及唯一的 waterfall 事件 `request/error`。

### Session 是唯一事实源

`Session.record_list` 是全部过程记录。模型输入的消息面**不缓存**，每次请求时从记录派生（`derive_messages()`）；步数预算、重试次数、turn 终态同理，都是**从账本现算**——loop 不持存这些状态，因此崩溃恢复后重算结果不变。

对应地有三类结构化账本：

| 记录 | 含义 | 查询接口 |
| --- | --- | --- |
| `llm/retry` | 第几次重试、因何决策、触发它的错误 | `Session.llm_retry_count(turn)` |
| `agent/grant` | 放宽了多少步预算 | `Session.granted_steps(turn)` |
| `agent/error-handle` | 每个错误被处置成了什么 | `Session.error_handles(turn)` |

落盘由 `SessionPresist` 订阅 `session/event` 异步批量完成（约 2 秒一批），失败时回滚文件长度后重试。

### turn 终态

`turn/end` 上的 `reason_type` 三选一，判据是**控制流事实而非"过程中有没有出错"**——出过错但被处置吸收（退避重试成功、放宽预算后继续）仍算 `success`：

| 取值 | 含义 |
| --- | --- |
| `success` | 正常完成（含错误被处置成"继续"后收敛） |
| `interrupted` | 用户取消，或被 `break` 但未声明 `as_error` 的策略终止 |
| `error` | 任何上抛的异常：`as_error` 的 break、无人认领、重试耗尽 |

详见 `CONTEXT.md` 的「turn 终态」条目。

### 错误处置

异常类型只表达**来源/归属**，不表达**如何恢复**——恢复策略是易变信息，放在独立的策略注册表（`agent/llm/llm_retry.py` 的 `LLMRerty`，以及 HITL 的订阅方）里，将来某类错误变可重试只需改表，不动异常树。

一次错误的处置链路：

```
step 捕获异常
  → waterfall("request/error")  取裁决 dict
  → 先落 agent/error-handle 账本（无论是谁上抛，都要留痕）
  → hand_decision 归一成控制信号
       retry / backoff_retry  → 记 llm/retry、按指数退避等待、continue
       continue（带 grant）   → 记 agent/grant、continue
       break                  → 结束本 turn；带 as_error 则原样上抛原错误
       无人认领               → 抛 AgentUnclaimedError
```

订阅方只表达**意图**（如 `{"decision": "continue", "grant": {"steps": 5}}`），状态变更由 loop 落成账本——执行权始终在 loop 手里。

## 扩展点

- **加工具**：继承 `Tool`（实现 `name` / `description` / `parameters` / `async execute`），实例化后交给 `ToolRegister.register()`。同名跳过并 warning，不覆盖。
- **加事件**：在 `eventspec.py` 登记 `EventSpec`，定义 payload。
- **加策略**：向事件总线 `register` 一个订阅方——错误重试策略挂在 `request/error`，工具审批护栏挂在 `tool/call`，人在回路同理。

## 已知未落地项

读代码时别被名字骗到，以下目前是空壳或未实现：`agent/guard/tool_user_guard.yaml`（0 字节，`read_yaml` 无调用点，护栏配置目前由调用方直接传 dict）、`core/app_context.py`、`loop_control.py`、`SessionStore.fork`、`Session.compact`、`EventService.dispatch`。

另有两处历史遗留待清理：`execption.py` 是拼写笔误（已被多处 import）；`agent/core/agent/loopcopy.py`、`deprecated_loop.py`、`session/deprecated_session.py` import 的旧符号已不存在，不可导入。

## 文档

`AGENTS.md`（= `CLAUDE.md`）定义了按需加载文档的规则，`CONTEXT.md` 是领域术语表。改代码前先按规则读 `docs/` 下的 `coding-rules/`、`specs/`、`flows/`。
