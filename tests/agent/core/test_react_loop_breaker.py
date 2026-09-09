"""ReActAgentLoop 的熔断与步数兜底行为测试。

覆盖（对应 架构设计/工具调用.md 的决策点）：
1. raise_on_break 工具触发熔断 → ToolConsecutiveFailureError 经 TaskGroup 以
   ExceptionGroup 上抛，loop 接住：记录 request/error、终止本 turn、不外抛
2. step_limit 兜底：工具未配置熔断时，达到最大步数强制终止 while True
3. schema_hide 熔断后下一次请求的 schema 不含该工具（request/header 记
   reason=change）；turn/end 事件清空熔断状态，工具恢复
"""

from types import SimpleNamespace
from typing import Any

import pytest

from myagent.agent.core.agent.loop import ReActAgentLoop
from myagent.agent.core.provider import (
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
    Usage,
)
from myagent.agent.core.session.session import Session
from myagent.agent.core.session.types import SessionMetaData
from myagent.agent.core.systemprompt.systemprompt import SystemPrompt
from myagent.agent.core.tool.register import ToolRegister
from myagent.agent.core.tool.tool import Tool
from myagent.agent.execption import ToolConsecutiveFailureError
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
    """组装被测 ReActAgentLoop：真实 SystemPrompt + 假 Provider + 熔断工具。

    AgentConfig 的字段仍在重构中（当前定义只有 step_limit，而 loop 还引用
    prompt_render_parame），故用 SimpleNamespace 同时提供两个字段。
    """
    event_service = EventService()
    session = Session(EventService(), SessionMetaData(cwd="."))
    session.presistence = NoopPersistence()
    register = ToolRegister()
    register.register(tool)
    config = SimpleNamespace(step_limit=step_limit, prompt_render_parame={})
    provider = FakeProvider(rounds)
    loop = ReActAgentLoop(event_service, session, register, SystemPrompt(), config, provider)
    return loop, provider, register


def tool_round(call_id: str) -> list:
    return [LLMResponse(content=None, tool_call_requests=[
        ToolCallRequest(id=call_id, name="flaky", arguments={})
    ], usage=Usage())]


def text_round(content: str) -> list:
    return [LLMResponse(content=content, usage=Usage())]


@pytest.mark.asyncio
async def test_熔断抛错记入step_error_触发一次request_error后循环继续():
    """raise_on_break 工具第 5 次失败触发熔断抛错：loop 只记入 step_error 并触发
    一次 request/error（waterfall 控制信号挂点），不终止循环；turn 由模型收敛结束"""
    tool = FlakyTool(max_consecutive_failures=5, raise_on_break=True)
    # 5 轮工具调用（第 5 轮触发熔断抛错），第 6 轮模型返回纯文本收敛结束
    rounds = [tool_round(f"call_{i}") for i in range(5)] + [text_round("工具不可用了")]
    loop, provider, _ = make_loop(tool, rounds, step_limit=20)

    errors = []
    # REQUEST_ERROR 是 waterfall 语义事件，回调需接受 (payload, next) 两个参数；
    # 必须用具名局部函数注册（EventService 弱引用 lambda 会立即失效）
    def on_error(payload, nxt):
        errors.append(payload.error_type)
    loop._event_service.register(REQUEST_ERROR.name, on_error)

    await loop.send("触发熔断")  # 不外抛，send 正常返回

    # 抛错后循环继续：第 6 次模型请求拿到纯文本，turn 正常收敛
    assert provider.calls == 6, "熔断抛错不应终止循环，第 6 次请求应正常发生"
    # 一次错误只触发一次 request/error（不因后续轮次重复触发）
    assert len(errors) == 1
    assert isinstance(errors[0], ToolConsecutiveFailureError)


@pytest.mark.asyncio
async def test_步数兜底_未配置熔断时达到上限强制终止():
    """工具未配置熔断（None）：连续失败不熔断，由 step_limit 强制终止 while True"""
    tool = FlakyTool()  # max_consecutive_failures=None
    rounds = [tool_round(f"call_{i}") for i in range(20)]
    loop, provider, _ = make_loop(tool, rounds, step_limit=5)

    errors = []
    def on_error(payload, nxt):
        errors.append(payload.error_type)
    loop._event_service.register(REQUEST_ERROR.name, on_error)

    await loop.send("测试步数兜底")

    assert provider.calls == 5, "恰好执行 step_limit 次模型请求后强制终止"
    assert len(errors) == 1
    assert "达到最大步数" in str(errors[0])


@pytest.mark.asyncio
async def test_熔断后schema过滤_turn结束自动恢复():
    """schema_hide：熔断后下一次请求的 schema 不含该工具；turn/end 后工具恢复"""
    tool = FlakyTool(max_consecutive_failures=5)  # 默认 schema_hide，不抛错
    # 5 轮工具调用（第 5 轮触发熔断），第 6 轮模型返回纯文本结束 turn
    rounds = [tool_round(f"call_{i}") for i in range(5)] + [text_round("工具不可用了")]
    loop, provider, register = make_loop(tool, rounds, step_limit=20)

    await loop.send("触发 schema 熔断")

    # 两次 request/header：初始 + 熔断后 schema 变化（reason=change）
    headers = [r for r in loop._session.record_list if r.type == "request/header"]
    assert len(headers) == 2
    assert headers[0].data.reason == "initial"
    assert [s["function"]["name"] for s in headers[0].data.tools] == ["flaky"]
    assert headers[1].data.reason == "change"
    assert headers[1].data.tools == [], "熔断后 schema 应过滤掉该工具"

    # turn/end 事件已触发 reset_breaker（loop 构造时接线）：工具恢复可用
    assert register.to_schemas() == [tool.to_schema()]
