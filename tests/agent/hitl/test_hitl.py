"""HITL 订阅方的行为测试。

覆盖两条契约：
1. 管辖范围——只认领自己的错误类型，其余必须 await 委托给链上下一个订阅方
   （见 docs/coding-rules/2026-09-18-waterfall订阅契约.md）
2. 裁决形状——选"继续"时返回的 grant 是**放宽预算的意图**，不是直接改 loop 的状态；
   loop 收到后落成 agent/grant 账本记录（见 loop.hand_decision）
"""

import asyncio

import pytest

from myagent.agent.execption import LLMRateLimitError, MaxStepsExceededError
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
