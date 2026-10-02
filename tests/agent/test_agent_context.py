"""AgentContext 隔离测试。

命题：上下文是 agent 的事件隔离边界——一个上下文上的订阅不接收另一个上下文的广播。

隔离落点是"谁持有 EventService"：每个 AgentContext 在构造时各建一份总线，agent 内的
订阅都注册在这份上（见 docs/adr/2026-10-02-AgentContext组件归属与事件隔离.md 决策 1）。
"""
from typing import Any

import pytest

from myagent.agent.agent_context import AgentContext
from myagent.agent.core.agent.types import AgentConfig
from myagent.agent.core.provider import RawToolCall
from myagent.agent.core.session.session import Session
from myagent.agent.core.session.types import SessionMetaData
from myagent.agent.core.tool.tool import Tool, ToolErrorType
from myagent.infra.events.eventspec import TURN_END
from myagent.infra.events.payload import TurnEndPayload

# Tool 的连续失败下限，见 core/tool/tool.py 的 MIN_CONSECUTIVE_FAILURES
BREAKER_THRESHOLD = 5


class FlakyTool(Tool):
    """永远执行失败、按 execute_intercept 熔断的工具。

    熔断模式下 execute 入口直接返回 BREAKER_INTERCEPT，是公开可观测的状态。
    """

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
        raise RuntimeError("总是失败")


class NoopProvider:
    """占位 Provider：本测试不驱动模型，任何调用都视为错误。"""

    def __init__(self):
        self.model = "noop"
        self.params = None

    async def chat(self, messages, tools=None):
        raise AssertionError("本测试不应调用模型")

    async def stream_chat(self, messages, tools=None):
        raise AssertionError("本测试不应调用模型")
        yield  # 使函数成为 async generator，与真实 Provider 的签名一致


class NoopPersistence:
    """替身持久化：Session.append 投喂记录、loop.persist_session_now 触发落盘，均不做事。"""

    def cache_data(self, record):
        pass

    def presist(self):
        pass


def make_context(name: str) -> AgentContext:
    """组装一个被测上下文：真实 AgentContext，模型与持久化用最小替身。

    内存态 Session（无 session_file）bind 后不会创建持久化组件，因此构造本身
    不产生后台任务；随后补一个替身，避免 loop 收尾时碰到 None。
    """
    ctx = AgentContext(
        agent_name=name,
        session=Session(SessionMetaData(cwd=".")),
        agent_config=AgentConfig(step_limit=10, max_retry_count=2),
        llm_client=NoopProvider(),
        prompt_render_parame=None,
    )
    ctx.session.presistence = NoopPersistence()
    ctx.agent_loop.tool_register.register(
        FlakyTool(max_consecutive_failures=BREAKER_THRESHOLD, breaker_mode="execute_intercept")
    )
    return ctx


def test_两个上下文_同一事件名的订阅互不触发():
    """测试场景：A、B 各自订阅 turn/end，A 广播后只有 A 的回调被调用；B 广播同理"""
    ctx_a = make_context("a")
    ctx_b = make_context("b")

    seen_a: list = []
    seen_b: list = []

    # 具名本地函数：EventService 以弱引用持有回调，匿名 lambda 会立即被回收
    def on_a(payload):
        seen_a.append(payload)

    def on_b(payload):
        seen_b.append(payload)

    ctx_a.event_service.register(TURN_END.name, on_a)
    ctx_b.event_service.register(TURN_END.name, on_b)

    payload_a = TurnEndPayload()
    ctx_a.event_service.emit(TURN_END.name, payload_a)

    assert seen_a == [payload_a]
    assert seen_b == []

    payload_b = TurnEndPayload()
    ctx_b.event_service.emit(TURN_END.name, payload_b)

    assert seen_b == [payload_b], "B 广播应触发 B 自己的回调"
    assert seen_a == [payload_a], "B 广播不得二次触发 A 的回调"


# ---------------------------------------------------------------------------
# 接入 ReActAgentLoop：一轮 turn 结束的复位只作用于本 agent
# ---------------------------------------------------------------------------


async def trip_breaker(loop, tag: str) -> None:
    """连续失败达到阈值，把该 loop 的工具打进熔断，并确认已生效。"""
    for i in range(BREAKER_THRESHOLD):
        await loop.tool_register.execute(RawToolCall(id=f"{tag}-{i}", name="flaky", arguments="{}"))
    hit = await loop.tool_register.execute(RawToolCall(id=f"{tag}-trip", name="flaky", arguments="{}"))
    assert hit.error_type is ToolErrorType.BREAKER_INTERCEPT, "前置条件：工具应已熔断"


async def breaker_is_open(loop, tag: str) -> bool:
    """熔断是否仍然生效：execute 入口是否仍返回 BREAKER_INTERCEPT。"""
    result = await loop.tool_register.execute(RawToolCall(id=f"{tag}-probe", name="flaky", arguments="{}"))
    return result.error_type is ToolErrorType.BREAKER_INTERCEPT


@pytest.mark.asyncio
async def test_一轮turn结束_只复位本agent的工具熔断():
    """测试场景：两个 agent 各自的工具都已熔断，A 的一轮结束时只复位 A 的，B 的不受影响

    loop 在构造时把 ToolRegister.reset_breaker 接到 TURN_END 上；总线若被共享，
    一个 agent 的轮次边界会清空另一个 agent 的熔断状态。
    """
    ctx_a = make_context("a")
    ctx_b = make_context("b")
    loop_a, loop_b = ctx_a.agent_loop, ctx_b.agent_loop

    await trip_breaker(loop_a, "a")
    await trip_breaker(loop_b, "b")

    # A 的一轮结束
    ctx_a.event_service.emit(TURN_END.name, TurnEndPayload())

    assert not await breaker_is_open(loop_a, "a-after"), "A 自己的轮次结束应复位 A 的熔断"
    assert await breaker_is_open(loop_b, "b-after"), "A 的轮次结束不得复位 B 的熔断"

    # 对照：B 自己的一轮结束才复位 B 的
    ctx_b.event_service.emit(TURN_END.name, TurnEndPayload())
    assert not await breaker_is_open(loop_b, "b-recover"), "B 自己的轮次结束应复位 B 的熔断"
