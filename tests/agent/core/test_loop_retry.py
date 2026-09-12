"""ReActAgentLoop 重试决策与退避延迟的行为测试。

覆盖（对应 ADR 2026-09-10-LLM重试延迟退避策略）：
1. mock provider 连续抛两个不同策略的错误——429 限流（backoff_retry）与 401 认证
   失败（dont_retry）：只有前者记 llm/retry 并退避等待，后者直接跳过，策略分流正确
2. 连续 backoff_retry 直到 max_retry_count： 并保留最后
   一次原始错误（重试次数 + 限流原因，供界面展示 10/10 之类信息）
"""

import asyncio
from types import SimpleNamespace

import pytest

from myagent.agent.core.agent.loop import ReActAgentLoop
from myagent.agent.core.provider import LLMProvider, LLMResponse, Message, Usage
from myagent.agent.core.session.session import Session
from myagent.agent.core.session.types import SessionMetaData
from myagent.agent.core.systemprompt.systemprompt import SystemPrompt
from myagent.agent.execption import LLMAuthError, LLMRateLimitError
from myagent.agent.llm.llm_retry import LLMRerty
from myagent.infra.events.eventspec import REQUEST_ERROR
from myagent.infra.events.service import EventService


class ScriptedErrorProvider(LLMProvider):
    """按脚本逐次给出结果的假 Provider：异常则抛出、LLMResponse 则产出、
    字符串 "hang" 则挂起（供取消用例触发 CancelledError）。"""

    def __init__(self, script: list):
        super().__init__(model="fake-model")
        self._script = list(script)
        self.calls = 0

    async def chat(self, messages, tools=None):
        raise NotImplementedError

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        if not self._script:
            raise RuntimeError("ScriptedErrorProvider 脚本耗尽，loop 仍在继续调模型")
        item = self._script.pop(0)
        if item == "hang":
            await asyncio.sleep(3600)  # 挂起直到被取消
            return
        if isinstance(item, Exception):
            raise item
        yield item


class NoopPersistence:
    def presist(self):
        pass


def make_loop(script: list, max_retry_count: int, strategy: LLMRerty):
    """组装被测 loop：真实策略注册表 LLMRerty + 假 Provider（按脚本抛错/返回）。

    strategy 由调用方持有，保证 EventService 的弱引用注册在用例期间不失效。
    """
    event_service = EventService()
    session = Session(EventService(), SessionMetaData(cwd="."))
    session.presistence = NoopPersistence()
    config = SimpleNamespace(step_limit=10, max_retry_count=max_retry_count)
    provider = ScriptedErrorProvider(script)
    loop = ReActAgentLoop(event_service, session, SystemPrompt(), config, provider)
    event_service.register(REQUEST_ERROR.name, strategy.request_error_event)
    return loop, provider, session


def install_recording_delay(loop):
    """替换 retry_delay 记录每次等待调用（error, policy, attempt），避免测试真的 sleep。"""
    waits = []

    async def fake_retry_delay(error, policy, attempt):
        waits.append((error, policy, attempt))
        return 0.0

    loop.retry_delay = fake_retry_delay
    return waits


def retry_records(session):
    return [r for r in session.record_list if r.type == "llm/retry"]


def last_record(session, record_type):
    return [r for r in session.record_list if r.type == record_type][-1]


def user_message(text="你好"):
    return Message(role="user", content=text)


@pytest.mark.asyncio
async def test_两个错误_仅走重试的那个记记录并等待():
    """先 429 限流（backoff_retry）再 401 认证失败（dont_retry），第三次成功：
    只有限流那次记 llm/retry 并退避等待，认证失败不等待；最终 turn 记为 success"""
    strategy = LLMRerty()
    script = [
        LLMRateLimitError("429 限流"),
        LLMAuthError("401 认证失败"),
        LLMResponse(content="最终回复", usage=Usage()),
    ]
    loop, provider, session = make_loop(script, max_retry_count=5, strategy=strategy)
    waits = install_recording_delay(loop)

    await loop.turn(user_message("你好"))

    assert provider.calls == 3, "两个错误后模型第三次返回成功"
    records = retry_records(session)
    assert len(records) == 1, "只有走重试的错误才记 llm/retry"
    assert records[0].data.retry_count == 1
    assert records[0].data.reason == "backoff_retry"
    assert len(waits) == 1, "只有走重试的错误才退避等待"
    assert waits[0][2] == 1, "第 1 次重试"
    assert waits[0][1].base_delay == 1.0, "取 backoff 档的退避参数"
    # 中间轮次的重试不算 error：最终成功，step 与 turn 都记 success
    assert all(r.data.reason_type == "success" for r in session.record_list if r.type == "step/end")
    assert last_record(session, "turn/end").data.reason_type == "success"


@pytest.mark.asyncio
async def test_连续重试达上限_step与turn记为error():
    """连续 429 限流直到 max_retry_count：不再抛异常，step 与 turn 双双记为 error，
    reason_text 保留重试次数与最后一次原始错误（供界面展示 10/10 与限流原因）"""
    strategy = LLMRerty()
    script = [LLMRateLimitError("429 限流") for _ in range(3)]
    loop, provider, session = make_loop(script, max_retry_count=2, strategy=strategy)
    install_recording_delay(loop)

    await loop.turn(user_message("你好"))  # 重试耗尽不再上抛，正常返回

    assert provider.calls == 3, "上限 2 次重试后第 3 次调用失败即耗尽"
    assert len(retry_records(session)) == 2, "耗尽前正常记了 2 次 llm/retry"
    step_ends = [r for r in session.record_list if r.type == "step/end"]
    assert [r.data.reason_type for r in step_ends] == ["success", "success", "error"]
    assert "2/2" in step_ends[-1].data.reason_text
    assert "429 限流" in step_ends[-1].data.reason_text, "保留原始错误信息"
    turn_end = last_record(session, "turn/end")
    assert turn_end.data.reason_type == "error"
    assert "2/2" in turn_end.data.reason_text and "429 限流" in turn_end.data.reason_text


@pytest.mark.asyncio
async def test_用户取消_step与turn记为interrupted():
    """模型调用挂起时取消任务：step 与 turn 记 interrupted，reason_text 为用户手动取消"""
    strategy = LLMRerty()
    loop, provider, session = make_loop(["hang"], max_retry_count=3, strategy=strategy)

    task = asyncio.create_task(loop.turn(user_message("你好")))
    await asyncio.sleep(0.01)  # 让任务进入挂起的模型调用
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task  # 取消原样上抛（step 已收敛为 interrupted）

    step_ends = [r for r in session.record_list if r.type == "step/end"]
    assert [r.data.reason_type for r in step_ends] == ["interrupted"]
    assert step_ends[-1].data.reason_text == "用户手动取消"
    turn_end = last_record(session, "turn/end")
    assert turn_end.data.reason_type == "interrupted"
    assert turn_end.data.reason_text == "用户手动取消"
