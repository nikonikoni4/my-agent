"""策略注册契约：AgentContext.register_policy 的注册、校验与去重。"""
import logging

import pytest

from myagent.agent.agent_context import AgentContext, AgentPolicySpec
from myagent.infra.events import EventService
from myagent.infra.events.eventspec import SESSION_EVENT


class Policy:
    def __init__(self):
        self.received = []

    def on_event(self, payload):
        self.received.append(payload)


def make_context() -> AgentContext:
    """绕过重量级构造：只装配策略注册用到的字段（总线 + 已注册对象表）。"""
    ctx = AgentContext.__new__(AgentContext)
    ctx.policy_objects = []
    ctx.event_service = EventService()
    return ctx


def test_绑定方法正常注册并派发():
    policy = Policy()
    ctx = make_context()
    ctx.register_policy(AgentPolicySpec(SESSION_EVENT, [policy.on_event], policy))
    ctx.event_service.emit(SESSION_EVENT.name, "received")
    assert policy.received == ["received"]
    assert ctx.policy_objects[0] is policy


@pytest.mark.parametrize("callback_kind", ["unbound", "foreign", "function", "noncallable"])
def test_拒绝非本实例绑定方法(callback_kind):
    policy, other = Policy(), Policy()
    callbacks = {
        "unbound": Policy.on_event,
        "foreign": other.on_event,
        "function": lambda payload: None,
        "noncallable": None,
    }
    with pytest.raises(ValueError, match="绑定方法"):
        AgentPolicySpec(SESSION_EVENT, [callbacks[callback_kind]], policy)


def test_拒绝空回调列表():
    with pytest.raises(ValueError, match="为空"):
        AgentPolicySpec(SESSION_EVENT, [], Policy())


def test_重复注册仅警告且不重复派发(caplog):
    ctx = make_context()
    policy = Policy()
    spec = AgentPolicySpec(SESSION_EVENT, [policy.on_event], policy)
    ctx.register_policy(spec)
    with caplog.at_level(logging.WARNING):
        ctx.register_policy(spec)
    ctx.event_service.emit(SESSION_EVENT.name, "once")
    assert "Policy 已经注册过" in caplog.text
    assert policy.received == ["once"]
    assert len(ctx.policy_objects) == 1


def test_相等但不同实例可以分别注册():
    class EqualPolicy(Policy):
        def __eq__(self, other):
            return True

    ctx = make_context()
    first, second = EqualPolicy(), EqualPolicy()
    ctx.register_policy([
        AgentPolicySpec(SESSION_EVENT, [first.on_event], first),
        AgentPolicySpec(SESSION_EVENT, [second.on_event], second),
    ])
    ctx.event_service.emit(SESSION_EVENT.name, "both")
    assert first.received == second.received == ["both"]
    assert len(ctx.policy_objects) == 2
