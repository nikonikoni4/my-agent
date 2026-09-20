"""HITL 订阅方的行为测试。

覆盖两条契约：
1. 管辖范围——只认领自己的错误类型，其余必须 await 委托给链上下一个订阅方
   （见 docs/coding-rules/2026-09-18-waterfall订阅契约.md）
2. 裁决形状——选"继续"时返回的 grant 是**放宽预算的意图**，不是直接改 loop 的状态；
   loop 收到后落成 agent/grant 账本记录（见 loop.hand_decision）
"""

import asyncio

import pytest

from myagent.agent.execption import (
    LLMRateLimitError,
    MaxStepsExceededError,
    ToolConsecutiveFailureError,
)
from myagent.agent.hitl.hitl import HITL
from myagent.agent.hitl.types import HumanReturn
from myagent.infra.events.payload import RequestErrorPayLoad


class ScriptedChannel:
    """脚本化的人机通道：固定返回一个选项；delay>0 时用于触发超时。"""

    def __init__(self, choice_id: str, delay: float = 0.0):
        self.choice_id = choice_id
        self.delay = delay
        self.asked = []

    async def ask_human(self, message):
        self.asked.append(message)
        if self.delay:
            await asyncio.sleep(self.delay)
        return HumanReturn(choice_id=self.choice_id, content="")


def make_next():
    """下游订阅方替身：被委托时记一次调用，并给出可辨识的裁决值。"""
    calls = []

    async def _next():
        calls.append(1)
        return {"decision": "retry"}

    return calls, _next


def error_payload(error) -> RequestErrorPayLoad:
    return RequestErrorPayLoad(error_type=error)


@pytest.mark.asyncio
async def test_不管辖的错误委托下游():
    """非最大步数错误（如限流）不认领：await 委托链上下一个订阅方，且不打扰人类"""
    channel = ScriptedChannel("continue")
    hitl = HITL(channel)
    calls, _next = make_next()

    result = await hitl.maxstep_continue(error_payload(LLMRateLimitError("429 限流")), _next)

    assert result == {"decision": "retry"}
    assert calls == [1], "把裁决权交给下游"
    assert channel.asked == [], "不管辖的错误不该弹窗"


@pytest.mark.asyncio
async def test_选继续_返回continue并带授予步数():
    """人类选"继续"：裁决为 continue，并携带放宽 N 步预算的意图（由 loop 落账）"""
    hitl = HITL(ScriptedChannel("continue"), grant_steps=5)
    _, _next = make_next()

    result = await hitl.maxstep_continue(error_payload(MaxStepsExceededError("超限")), _next)

    assert result == {"decision": "continue", "grant": {"steps": 5}}


@pytest.mark.asyncio
async def test_选取消_返回break():
    """人类选"取消"：裁决为 break，不携带授予（未放宽预算就不该记账）"""
    hitl = HITL(ScriptedChannel("break"))
    _, _next = make_next()

    result = await hitl.maxstep_continue(error_payload(MaxStepsExceededError("超限")), _next)

    assert result == {"decision": "break"}
    assert "grant" not in result


@pytest.mark.asyncio
async def test_人类不回应_超时按break收束():
    """人类长时间不回应：超时后按 break 收束，不把 loop 挂在等待上"""
    hitl = HITL(ScriptedChannel("continue", delay=1.0), timeout=0.01)
    _, _next = make_next()

    result = await hitl.maxstep_continue(error_payload(MaxStepsExceededError("超限")), _next)

    assert result == {"decision": "break"}


# ---------------------------------------------------------------------------
# 工具熔断：raise_on_break 配置下 ToolRegister 抛出 ToolConsecutiveFailureError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_熔断_非熔断错误委托下游():
    """限流之类的错误不归熔断处理管：await 委托下游，不弹窗"""
    channel = ScriptedChannel("continue")
    hitl = HITL(channel)
    calls, _next = make_next()

    result = await hitl.tool_breaker_continue(error_payload(LLMRateLimitError("429")), _next)

    assert result == {"decision": "retry"}
    assert calls == [1]
    assert channel.asked == []


@pytest.mark.asyncio
async def test_熔断_裸异常被认领():
    """熔断错直接上抛时被认领，选"继续"→ continue（不带 grant：熔断与步数预算无关）"""
    channel = ScriptedChannel("continue")
    hitl = HITL(channel)
    _, _next = make_next()

    result = await hitl.tool_breaker_continue(
        error_payload(ToolConsecutiveFailureError("连续失败 3 次触发熔断")), _next
    )

    assert result == {"decision": "continue"}
    assert len(channel.asked) == 1


@pytest.mark.asyncio
async def test_熔断_包在ExceptionGroup里仍被认领():
    """关键：熔断错与同批其他异常一起时被 TaskGroup 包成 ExceptionGroup 上抛，
    payload.error_type 是 group 而非熔断错本身——仍须认领"""
    channel = ScriptedChannel("continue")
    hitl = HITL(channel)
    _, _next = make_next()
    group = ExceptionGroup("批失败", [
        ToolConsecutiveFailureError("连续失败 3 次触发熔断"),
        RuntimeError("同批另一个工具炸了"),
    ])

    result = await hitl.tool_breaker_continue(error_payload(group), _next)

    assert result == {"decision": "continue"}
    assert len(channel.asked) == 1, "group 里含熔断错即认领"
    assert "熔断" in channel.asked[0].content


@pytest.mark.asyncio
async def test_熔断_不含熔断错的group委托下游():
    """非熔断的 ExceptionGroup（TaskGroup 包的普通工具异常）不认领，委托下游"""
    channel = ScriptedChannel("continue")
    hitl = HITL(channel)
    calls, _next = make_next()
    group = ExceptionGroup("批失败", [RuntimeError("甲")])

    result = await hitl.tool_breaker_continue(error_payload(group), _next)

    assert result == {"decision": "retry"}
    assert calls == [1]
    assert channel.asked == []


@pytest.mark.asyncio
async def test_熔断_选取消_终止并按失败上报():
    """人类选"取消"：熔断意味着该工具本轮不可用，终止并如实上报——
    break 带 as_error，使 turn 记 error 而非 success"""
    hitl = HITL(ScriptedChannel("break"))
    _, _next = make_next()

    result = await hitl.tool_breaker_continue(
        error_payload(ToolConsecutiveFailureError("熔断")), _next
    )

    assert result == {"decision": "break", "as_error": True}
