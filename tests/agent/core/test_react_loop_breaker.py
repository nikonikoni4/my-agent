"""ReActAgentLoop 的熔断与步数兜底行为测试。

覆盖（对应 架构设计/工具调用.md 的决策点）：
1. raise_on_break 工具触发熔断 → ToolConsecutiveFailureError 经 TaskGroup 以
   ExceptionGroup 上抛，loop 触发一次 request/error；无人认领 → 抛
   AgentUnclaimedError（cause 为该 group），turn/end 记 error
2. step_limit 兜底：工具未配置熔断时，达到最大步数抛 MaxStepsExceededError，
   它同样无人认领 → AgentUnclaimedError（cause 为 MaxStepsExceededError）
3. schema_hide 熔断后下一次请求的 schema 不含该工具（request/header 记
   reason=change）；turn/end 事件清空熔断状态，工具恢复
"""

from typing import Any

import pytest

from myagent.agent.core.agent.loop import ReActAgentLoop
from myagent.agent.core.agent.types import AgentConfig
from myagent.agent.core.provider import (
    LLMProvider,
    LLMResponse,
    Message,
    RawToolCall,
    Usage,
)
from myagent.agent.core.session.session import Session
from myagent.agent.core.session.types import SessionMetaData
from myagent.agent.core.systemprompt.systemprompt import SystemPrompt
from myagent.agent.core.tool.tool import Tool
from myagent.agent.execption import (
    AgentUnclaimedError,
    MaxStepsExceededError,
    ToolConsecutiveFailureError,
)
from myagent.infra.events.eventspec import REQUEST_ERROR
from myagent.infra.events.service import EventService


class FlakyTool(Tool):
    """永远执行失败的工具，用于触发熔断。"""

    def __init__(self, **breaker_kwargs):
        super().__init__(**breaker_kwargs)
        self.calls = 0

    @property
    def name(self) -> str:
        return "flaky"

    @property
    def description(self) -> str:
        return "永远失败的测试工具"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        self.calls += 1
        raise RuntimeError("总是失败")


class FakeProvider(LLMProvider):
    """按脚本逐轮返回的假 Provider：每轮返回一次工具调用或纯文本。"""

    def __init__(self, rounds: list[list]):
        super().__init__(model="fake-model")
        self._rounds = list(rounds)
        self.calls = 0

    async def chat(self, messages, tools=None):
        raise NotImplementedError

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        if not self._rounds:
            raise RuntimeError("FakeProvider 脚本耗尽，loop 仍在继续调模型")
        for item in self._rounds.pop(0):
            yield item


class NoopPersistence:
    def presist(self):
        pass


def make_loop(tool: Tool, rounds: list[list], step_limit: int):
    """组装被测 ReActAgentLoop：真实 SystemPrompt + 假 Provider + 熔断工具。"""
    event_service = EventService()
    session = Session(EventService(), SessionMetaData(cwd="."))
    session.presistence = NoopPersistence()
    config = AgentConfig(step_limit=step_limit, max_retry_count=2)
    provider = FakeProvider(rounds)
    loop = ReActAgentLoop(event_service, session, SystemPrompt(), config, provider)
    loop.tool_register.register(tool)
    return loop, provider, loop.tool_register


def tool_round(call_id: str) -> list:
    return [LLMResponse(content=None, tool_call_requests=[
        RawToolCall(id=call_id, name="flaky", arguments="{}")
    ], usage=Usage())]


def text_round(content: str) -> list:
    return [LLMResponse(content=content, usage=Usage())]


def user_message(text="你好"):
    return Message(role="user", content=text)


def leaves(error) -> list[BaseException]:
    """展开 ExceptionGroup，取出全部叶子异常（TaskGroup 抛出的 group 需要它才能断言内层）。"""
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in leaves(child)]
    return [error]


@pytest.mark.asyncio
async def test_熔断抛错_无人认领上抛():
    """raise_on_break 工具第 5 次失败触发熔断抛错：经 TaskGroup 以 ExceptionGroup 上抛，
    loop 触发一次 request/error（订阅方不给出决策）→ 抛 AgentUnclaimedError，cause 为该 group"""
    tool = FlakyTool(max_consecutive_failures=5, raise_on_break=True)
    rounds = [tool_round(f"call_{i}") for i in range(5)]
    loop, provider, _ = make_loop(tool, rounds, step_limit=20)

    errors = []
    # REQUEST_ERROR 是 waterfall 语义事件，回调需接受 (payload, next) 两个参数，
    # 并按契约返回决策（loop 消费 {"decision": ...} 控制信号）；返回 None 表示无人认领
    # 必须用具名局部函数注册（EventService 弱引用 lambda 会立即失效）
    def on_error(payload, nxt):
        errors.append(payload.error_type)
        return None

    loop._event_service.register(REQUEST_ERROR.name, on_error)

    with pytest.raises(AgentUnclaimedError) as excinfo:
        await loop.turn(user_message("触发熔断"))

    # 第 5 次工具调用触发熔断：抛错即终止本 turn，不再有第 6 次模型请求
    assert provider.calls == 5
    # 一次错误只触发一次 request/error
    assert len(errors) == 1
    assert isinstance(errors[0], ExceptionGroup)
    assert [type(e) for e in leaves(errors[0])] == [ToolConsecutiveFailureError]
    assert excinfo.value.__cause__ is errors[0]


@pytest.mark.asyncio
async def test_步数兜底_未配置熔断时达到上限上抛():
    """工具未配置熔断（None）：连续失败不熔断，由 step_limit 兜底——抛
    MaxStepsExceededError，它同样无人认领 → AgentUnclaimedError（cause 为前者）"""
    tool = FlakyTool()  # max_consecutive_failures=None
    rounds = [tool_round(f"call_{i}") for i in range(20)]
    loop, provider, _ = make_loop(tool, rounds, step_limit=5)

    errors = []

    def on_error(payload, nxt):
        errors.append(payload.error_type)
        return None

    loop._event_service.register(REQUEST_ERROR.name, on_error)

    with pytest.raises(AgentUnclaimedError) as excinfo:
        await loop.turn(user_message("测试步数兜底"))

    assert provider.calls == 5, "恰好执行 step_limit 次模型请求后强制终止"
    assert len(errors) == 1
    assert isinstance(errors[0], MaxStepsExceededError)
    assert "达到最大步数" in str(errors[0])
    assert excinfo.value.__cause__ is errors[0]


@pytest.mark.asyncio
async def test_熔断后schema过滤_turn结束自动恢复():
    """schema_hide：熔断后下一次请求的 schema 不含该工具；turn/end 后工具恢复"""
    tool = FlakyTool(max_consecutive_failures=5)  # 默认 schema_hide，不抛错
    # 5 轮工具调用（第 5 轮触发熔断），第 6 轮模型返回纯文本结束 turn
    rounds = [tool_round(f"call_{i}") for i in range(5)] + [text_round("工具不可用了")]
    loop, provider, register = make_loop(tool, rounds, step_limit=20)

    await loop.turn(user_message("触发 schema 熔断"))

    # 两次 request/header：初始 + 熔断后 schema 变化（reason=change）
    headers = [r for r in loop._session.record_list if r.type == "request/header"]
    assert len(headers) == 2
    assert headers[0].data.reason == "initial"
    assert [s["function"]["name"] for s in headers[0].data.tools] == ["flaky"]
    assert headers[1].data.reason == "change"
    assert headers[1].data.tools == [], "熔断后 schema 应过滤掉该工具"

    # turn/end 事件已触发 reset_breaker（loop 构造时接线）：工具恢复可用
    assert register.to_schemas() == [tool.to_schema()]


# ---------------------------------------------------------------------------
# 参数 JSON 解析失败（信任边界在工具层）：解析错误回喂模型自纠
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_非法JSON参数_工具不执行_回喂解析错误与原文():
    """模型输出的 arguments 是非法 JSON：ToolRegister 解析失败返回带 hint 的
    错误结果（工具本体不执行、计入熔断计数），以 role=tool 消息回喂，模型下一轮自纠"""
    tool = FlakyTool(max_consecutive_failures=5)
    rounds = [
        [LLMResponse(content=None, tool_call_requests=[
            RawToolCall(id="call_bad", name="flaky", arguments='{"date": "2026-09-02"')
        ], usage=Usage())],
        text_round("参数写错了，我重新调用"),
    ]
    loop, provider, _ = make_loop(tool, rounds, step_limit=10)

    await loop.turn(user_message("测试非法 JSON"))

    assert tool.calls == 0, "解析失败不应执行工具本体"
    # 熔断计数在 turn/end 的 reset_breaker 后已清空（不跨 turn），
    # 计入与否由 register 单测（test_tool_and_register.py）覆盖
    tool_results = [r for r in loop._session.record_list if r.type == "tool/result"]
    assert len(tool_results) == 1
    content = tool_results[0].data.message.content
    assert "不是合法 JSON" in content
    assert '{"date": "2026-09-02"' in content, "原文回显供模型定位错误"
    assert "被截断" not in content, "finish_reason 非 length 不应带截断 hint"
    assert provider.calls == 2, "回喂解析错误后模型下一轮收敛"


@pytest.mark.asyncio
async def test_截断的非法JSON_回喂结果带截断hint():
    """finish_reason=length 的截断调用（provider 打 truncated 标记）：解析失败
    的回喂话术走截断分支（精简参数/拆分调用），区别于普通语法错误"""
    tool = FlakyTool()
    rounds = [
        [LLMResponse(content=None, finish_reason="length", tool_call_requests=[
            # 模拟 provider 对 length 响应的提取结果：truncated=True
            RawToolCall(id="call_trunc", name="flaky", arguments='{"date": "2026-09-2', truncated=True)
        ], usage=Usage())],
        text_round("输出被截断了，我精简参数重新调用"),
    ]
    loop, _, _ = make_loop(tool, rounds, step_limit=10)

    await loop.turn(user_message("测试截断"))

    assert tool.calls == 0, "解析失败不应执行工具本体"
    tool_results = [r for r in loop._session.record_list if r.type == "tool/result"]
    assert len(tool_results) == 1
    content = tool_results[0].data.message.content
    assert "不是合法 JSON" in content
    assert "max_tokens" in content and "被截断" in content, "截断分支应给针对性 hint"
    assert "语法错误" not in content, "截断分支不应再给语法错误话术"
