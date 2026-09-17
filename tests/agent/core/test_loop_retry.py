"""ReActAgentLoop 重试决策与退避延迟的行为测试。

覆盖（对应 ADR 2026-09-10-LLM重试延迟退避策略 + 2026-09-15-agent-loop错误处理重构）：
1. mock provider 连续抛两个错误——429 限流（backoff_retry）与 401 认证失败
   （策略表已无对应档）：前者记 llm/retry 并退避等待，后者无人认领直接上抛
2. 连续 backoff_retry 直到 max_retry_count：抛 RetryExhaustedError，
   并保留最后一次原始错误（重试次数 + 限流原因，供界面展示 10/10 之类信息）
"""

import asyncio

import pytest

from myagent.agent.core.agent.loop import ReActAgentLoop
from myagent.agent.core.agent.types import AgentConfig
from myagent.agent.core.provider import LLMProvider, LLMResponse, Message, Usage
from myagent.agent.core.session.session import Session
from myagent.agent.core.session.types import SessionMetaData
from myagent.agent.core.systemprompt.systemprompt import SystemPrompt
from myagent.agent.execption import (
    AgentUnclaimedError,
    LLMAuthError,
    LLMRateLimitError,
    RetryExhaustedError,
)
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
    config = AgentConfig(step_limit=10, max_retry_count=max_retry_count)
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
    """先 429 限流（backoff_retry）再 401 认证失败（策略表无对应档）：限流那次记
    llm/retry 并退避等待，认证失败无人认领直接上抛（不再"跳过、继续下一条"）"""
    strategy = LLMRerty()
    script = [
        LLMRateLimitError("429 限流"),
        LLMAuthError("401 认证失败"),
        LLMResponse(content="最终回复", usage=Usage()),
    ]
    loop, provider, session = make_loop(script, max_retry_count=5, strategy=strategy)
    waits = install_recording_delay(loop)

    with pytest.raises(AgentUnclaimedError) as excinfo:
        await loop.turn(user_message("你好"))

    assert provider.calls == 2, "限流重试后第二次调用认证失败，随即终止"
    assert isinstance(excinfo.value.__cause__, LLMAuthError)
    records = retry_records(session)
    assert [r.data.reason for r in records] == ["backoff_retry"], \
        "只有走重试的那次落 llm/retry；无人认领未重试，不落记录，也不计入重试次数"
    assert records[0].data.retry_count == 1
    assert session.llm_retry_count(session.turn) == 1, "llm/retry 条数 = 实际重试次数"
    assert len(waits) == 1, "只有走重试的错误才退避等待"
    assert waits[0][2] == 1, "第 1 次重试"
    assert waits[0][1].base_delay == 1.0, "取 backoff 档的退避参数"
    assert [r.data.reason_type for r in session.record_list if r.type == "step/end"] == ["error", "error"]
    assert last_record(session, "turn/end").data.reason_type == "error"


@pytest.mark.asyncio
async def test_连续重试达上限_上抛并记error():
    """连续 429 限流直到 max_retry_count：抛 RetryExhaustedError，step 与 turn 双双记为
    error，reason_text 保留重试次数与最后一次原始错误（供界面展示 10/10 与限流原因）"""
    strategy = LLMRerty()
    script = [LLMRateLimitError("429 限流") for _ in range(3)]
    loop, provider, session = make_loop(script, max_retry_count=2, strategy=strategy)
    install_recording_delay(loop)

    with pytest.raises(RetryExhaustedError) as excinfo:
        await loop.turn(user_message("你好"))

    assert provider.calls == 3, "上限 2 次重试后第 3 次调用失败即耗尽"
    assert isinstance(excinfo.value.__cause__, LLMRateLimitError)
    assert [r.data.reason for r in retry_records(session)] == [
        "backoff_retry", "backoff_retry"
    ], "两次重试各记一条；耗尽的第三次未重试，不落记录（否则虚增重试次数）"
    assert session.llm_retry_count(session.turn) == 2
    step_ends = [r for r in session.record_list if r.type == "step/end"]
    assert [r.data.reason_type for r in step_ends] == ["error"] * 3, "每次失败的尝试各占一步"
    assert "429 限流" in step_ends[-1].data.reason_text, "step/end 记本步自己的错误"
    turn_end = last_record(session, "turn/end")
    assert turn_end.data.reason_type == "error"
    # 终态决策只在 turn/end：重试耗尽（RetryExhaustedError）+ 原始错误都在链上
    assert "2/2" in turn_end.data.reason_text and "429 限流" in turn_end.data.reason_text


@pytest.mark.asyncio
async def test_用户取消_step与turn记为interrupted():
    """模型调用挂起时取消任务：step 与 turn 记 interrupted，reason_text 为用户主动打断"""
    strategy = LLMRerty()
    loop, provider, session = make_loop(["hang"], max_retry_count=3, strategy=strategy)

    task = asyncio.create_task(loop.turn(user_message("你好")))
    await asyncio.sleep(0.01)  # 让任务进入挂起的模型调用
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task  # 取消原样上抛（step 已收敛为 interrupted）

    step_ends = [r for r in session.record_list if r.type == "step/end"]
    assert [r.data.reason_type for r in step_ends] == ["interrupted"]
    assert step_ends[-1].data.reason_text == "用户主动打断"
    turn_end = last_record(session, "turn/end")
    assert turn_end.data.reason_type == "interrupted"
    assert turn_end.data.reason_text == "用户主动打断"
