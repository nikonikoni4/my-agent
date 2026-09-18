"""ReActAgentLoop 的全部出路（退出路径）测试。

本文件顶部穷举当前 loop 的所有出路，随后每个出路至少一条用例。
术语：一条"出路"= 一次 turn/step 结束并落盘一条终态记录（step/end / turn/end）的路径。

本轮重构后 step 只有 2 个 except（取消 / 兜底，均只收集异常）+ 1 个 finally
（补全 → 记账 → 决策），终止统一以"抛出"表达，turn 由 except 得知终态。

== 入口 / 调度层 ==
E1  start() 启动后台循环（整个 loop 一个 task）；send 把消息写入 inbox["next_turn"] 并唤醒
E2  _loop 被唤醒后按序执行 turn 并消费 inbox（next_step 优先于 next_turn）
E3  _on_loop_done：loop 因未归一化异常终止 → 异常留在 loop task 上（可被调用方取回），
    并由回调 logger.error 留痕（防止静默失败）
E4  cancel() 打断在途 turn 并结束循环：step/turn 记 interrupted，取消传播到 _loop 后
    被消化（记 warning + break），loop task 正常结束、无 error 日志
E5  cancel() 落在空闲等待时：同样记 warning + break 结束循环；之后 send() 按需重启循环

== 正常收敛（不外抛） ==
P1  模型不再请求工具 → step/end success，turn/end success
P2  工具循环正常收敛（工具调用 → 结果回喂 → 收敛）→ success
P3  工具执行失败被工具层兜底（功能降级）→ 回喂错误结果，不上抛 → success
P4  参数 JSON 解析失败 → 回喂带 hint 结果，不上抛 → success

== 终止出口（统一抛出） ==
P5  用户取消（模型调用挂起）→ step/end、turn/end 记 interrupted，取消原样上抛
P6  达到 step_limit → MaxStepsExceededError 无人认领 → AgentUnclaimedError（cause 为前者）
P7  LLM 调用超时（TimeoutError）→ 无人认领 → AgentUnclaimedError
P8  401 认证失败（配置类错误，策略表不再覆盖）→ 无人认领 → AgentUnclaimedError，不等待
P9  429 限流 backoff_retry → 记 llm/retry + 退避；超限 → RetryExhaustedError（cause 为限流）
P10 LLMCallError 基类（来源未细分）→ 无人认领 → AgentUnclaimedError
P11 工具熔断 raise_on_break → ToolConsecutiveFailureError（经 ExceptionGroup）→ 无人认领 → 上抛
P12 非熔断 ExceptionGroup → 无人认领 → AgentUnclaimedError（cause 为 group）
P13 provider 内部 bug（RuntimeError）→ 无人认领 → AgentUnclaimedError（cause 为 RuntimeError）
P14 流中途失败：不补 finish 块（provider 只在正常结束产 finish），异常上抛

== 记录形态 ==
R1  llm/retry 只在真正重试时落（含 error_type + 异常链文本）；条数即本 turn 重试次数，
    unclaimed / exhausted 不落记录、不虚增计数
R2  异常链文本：逐层缩进、限深 3 层（超出显式标注）、展开 ExceptionGroup
R3  step_opened：预算在开步前拒绝的那一轮不写 step/end（step/start 与 step/end 严格配对）
R4  补全：异常打断工具调用时补齐占位 tool/result（is_error=True），避免非法消息面
R5  turn/end 的 error_type 承载"终止本 turn 的类别"（成功空串 / 耗尽 RetryExhaustedError），
    原错误只留在 reason_text 链里
R6  无人认领时类别也可查：error_type=AgentUnclaimedError，cause 在 reason_text 链中

请求组装的用例（System Prompt / System Reminder / runtime context 的落点）见文件末尾"请求组装"一节。

openai provider 的 mock 数据用例见 test_openai_provider_mock.py。
"""

import asyncio
import logging

import pytest

from myagent.agent.core.agent.loop import ReActAgentLoop, _format_error_chain
from myagent.agent.core.agent.types import AgentConfig
from myagent.agent.core.provider import (
    LLMProvider,
    LLMResponse,
    Message,
    RawToolCall,
    StreamChunk,
    Usage,
)
from myagent.agent.core.session.session import Session
from myagent.agent.core.session.store import SessionStore
from myagent.agent.core.session.types import SessionMetaData
from myagent.agent.core.systemprompt.systemprompt import SystemPrompt
from myagent.agent.core.systemprompt.types import PrompSection
from myagent.agent.core.tool.tool import Tool
from myagent.agent.execption import (
    AgentUnclaimedError,
    LLMAuthError,
    LLMCallError,
    LLMRateLimitError,
    MaxStepsExceededError,
    RetryExhaustedError,
    ToolConsecutiveFailureError,
)
from myagent.agent.llm.llm_retry import LLMRerty
from myagent.infra.events.eventspec import REQUEST_ERROR
from myagent.infra.events.service import EventService

LOOP_LOGGER = "myagent.agent.core.agent.loop"


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class ScriptedProvider(LLMProvider):
    """按脚本逐次产出结果的假 Provider。

    脚本元素含义（每次模型调用消费一个）：
      "hang"                —— 挂起（等待被取消）
      BaseException 实例    —— 直接抛出
      LLMResponse           —— 产出一个完整结果
      list                  —— 逐项产出；元素为 BaseException 时先产出前面的再抛出
    另记录每次调用收到的 messages，供调度顺序断言。
    """

    def __init__(self, script: list):
        super().__init__(model="fake-model")
        self._script = list(script)
        self.calls = 0
        self.seen_messages: list[list] = []

    async def chat(self, messages, tools=None):
        raise NotImplementedError

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        self.seen_messages.append(messages)
        if not self._script:
            raise RuntimeError("ScriptedProvider 脚本耗尽，loop 仍在继续调模型")
        item = self._script.pop(0)
        if item == "hang":
            await asyncio.sleep(3600)  # 挂起直到被取消/超时
            return
        for entry in (item if isinstance(item, list) else [item]):
            if isinstance(entry, BaseException):
                raise entry
            yield entry


class NoopPersistence:
    """替身持久化：loop.persist_session_now 会调用它，测试中不做落盘。"""

    def presist(self):
        pass


class OkTool(Tool):
    """总是成功的无参工具，用于构造正常工具循环。"""

    def __init__(self):
        super().__init__()

    @property
    def name(self) -> str:
        return "ok"

    @property
    def description(self) -> str:
        return "总是成功的测试工具"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        return "ok 结果"


class FlakyTool(Tool):
    """总是执行失败的工具，用于功能降级 / 熔断两条出路。"""

    def __init__(self, **breaker_kwargs):
        super().__init__(**breaker_kwargs)

    @property
    def name(self) -> str:
        return "flaky"

    @property
    def description(self) -> str:
        return "总是失败的测试工具"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        raise RuntimeError("总是失败")


# ---------------------------------------------------------------------------
# 组装与辅助
# ---------------------------------------------------------------------------


def make_loop(script, *, tools=None, step_limit=10, max_retry_count=2, retry_strategy=None):
    """组装被测 loop：真实 Session/ToolRegister/SystemPrompt + 脚本化假 Provider。

    session.presistence 必须给替身：persist_session_now 只吞 OSError，
    为 None 时抛 AttributeError 会被当成 turn 级异常，污染用例语义。
    retry_strategy 由调用方持有（EventService 弱引用，否则注册立即失效）。
    """
    event_service = EventService()
    session = Session(EventService(), SessionMetaData(cwd="."))
    session.presistence = NoopPersistence()
    config = AgentConfig(step_limit=step_limit, max_retry_count=max_retry_count)
    provider = ScriptedProvider(script)
    loop = ReActAgentLoop(
        event_service, session, SystemPrompt(), config, provider,
        prompt_render_parame={},
    )
    if tools is not None:
        loop.tool_register.register(tools)
    if retry_strategy is not None:
        event_service.register(REQUEST_ERROR.name, retry_strategy.request_error_event)
    return loop, provider, session


def install_recording_delay(loop):
    """替换 retry_delay 记录等待调用并返回 0，避免测试真的 sleep。"""
    waits = []

    async def fake_retry_delay(error, policy, attempt):
        waits.append((error, policy, attempt))
        return 0.0

    loop.retry_delay = fake_retry_delay
    return waits


def records(session, record_type):
    return [r for r in session.record_list if r.type == record_type]


def last(session, record_type):
    return records(session, record_type)[-1]


def leaves(error) -> list[BaseException]:
    """展开 ExceptionGroup，取出全部叶子异常（TaskGroup 抛出的 group 需要它才能断言内层）。"""
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in leaves(child)]
    return [error]


def user_message(text="你好"):
    return Message(role="user", content=text)


def tool_round(call_id: str, tool_name: str = "ok", arguments: str = "{}") -> list:
    """一轮"模型请求工具"的脚本片段。"""
    return [LLMResponse(content=None, tool_call_requests=[
        RawToolCall(id=call_id, name=tool_name, arguments=arguments)
    ], usage=Usage())]


async def wait_until(predicate, timeout: float = 2.0):
    """轮询等待条件成立（用于 _loop 这类后台任务，避免依赖固定 sleep 时长）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("wait_until 等待超时")
        await asyncio.sleep(0.005)


async def shutdown(loop, loop_task):
    """收尾：cancel 掉 loop 并等其 task 收敛。

    cancel() 的约定是"取消并结束循环"：取消在 _loop 内被消化后 break，任务以正常
    结束收场，因此这里直接 cancel 即可停住循环。gather(return_exceptions=True)
    顺带取回 task 异常，避免遗留 pending task 与 "exception was never retrieved"
    干扰后续用例。
    """
    loop.cancel()
    await asyncio.gather(loop_task, return_exceptions=True)


# ===========================================================================
# 入口 / 调度层
# ===========================================================================


@pytest.mark.asyncio
async def test_E1_send入inbox并唤醒_loop完成一轮():
    """start() 启动整个 loop 的单一 task；send 唤醒后跑完一轮，inbox 清空"""
    loop, provider, session = make_loop([LLMResponse(content="你好", finish_reason="stop")])
    loop_task = loop.start()
    try:
        await loop.send("你好", "next_turn")
        await wait_until(lambda: records(session, "turn/end"))
        assert provider.calls == 1
        assert loop.inbox["next_turn"] == []
        assert loop.inbox["next_step"] == []
        assert last(session, "turn/end").data.reason_type == "success"
    finally:
        await shutdown(loop, loop_task)


@pytest.mark.asyncio
async def test_E2_loop优先消费next_step():
    """inbox 中同时有 next_step 与 next_turn 时，next_step 的消息先被处理"""
    loop, provider, session = make_loop([
        LLMResponse(content="第一轮", finish_reason="stop"),
        LLMResponse(content="第二轮", finish_reason="stop"),
    ])
    loop_task = loop.start()
    try:
        loop.inbox["next_turn"].append(user_message("turn消息"))
        loop.inbox["next_step"].append(user_message("step消息"))
        loop._wakeup.set()
        await wait_until(lambda: len(records(session, "turn/end")) == 2)
        # 第一次模型调用看到的是 next_step 的消息（先被消费）
        first_seen = provider.seen_messages[0]
        assert any(getattr(m, "content", None) == "step消息" for m in first_seen)
    finally:
        await shutdown(loop, loop_task)


@pytest.mark.asyncio
async def test_E3_on_loop_done_异常终止记error日志(caplog):
    """loop 因未归一化异常终止时 _on_loop_done 取回异常并 logger.error（避免静默失败）；
    取回不减损可观测性：异常本身仍留在 loop task 上，调用方仍可取到"""
    loop, provider, session = make_loop([RuntimeError("provider boom")])
    loop_task = loop.start()
    try:
        with caplog.at_level(logging.ERROR, logger=LOOP_LOGGER):
            await loop.send("你好", "next_turn")
            await wait_until(lambda: "agent loop 任务异常终止" in caplog.text)
        error = loop_task.exception()
        assert isinstance(error, AgentUnclaimedError)
        assert isinstance(error.__cause__, RuntimeError)
        assert "provider boom" in caplog.text, "日志带上触发它的原错误"
        assert provider.calls == 1, "异常终止后循环停止，不再调模型"
        assert last(session, "turn/end").data.reason_type == "error"
    finally:
        await shutdown(loop, loop_task)


@pytest.mark.asyncio
async def test_E4_cancel打断在途turn并结束循环(caplog):
    """cancel() 在途取消：step/turn 记 interrupted，取消传播到 _loop 后被消化
    （记 warning + break），loop task 正常结束、无 error 日志"""
    loop, provider, session = make_loop(["hang"])
    loop_task = loop.start()
    with caplog.at_level(logging.WARNING, logger=LOOP_LOGGER):
        await loop.send("取消我", "next_turn")
        await wait_until(lambda: records(session, "step/start"))
        loop.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
    assert last(session, "step/end").data.reason_type == "interrupted"
    assert last(session, "turn/end").data.reason_type == "interrupted"
    assert loop_task.done(), "cancel() 应打断循环"
    assert loop_task.cancelled() is False, "取消在 _loop 内被消化，任务应以正常结束收场"
    assert "打断循环" in caplog.text
    assert "agent loop 任务异常终止" not in caplog.text, "取消已按 interrupted 收口，不应报异常"


@pytest.mark.asyncio
async def test_E5_空闲态cancel结束循环_可重启(caplog):
    """loop 空闲（挂在 _wakeup.wait()）时 cancel()：同样打断循环、任务正常结束；
    之后再 send() 会按需重启循环并正常处理消息"""
    loop, provider, session = make_loop([LLMResponse(content="你好", finish_reason="stop")])
    loop_task = loop.start()
    try:
        await asyncio.sleep(0)  # 让 _loop 先启动并挂到等待上（否则取消会落在"未启动"的协程上）
        with caplog.at_level(logging.WARNING, logger=LOOP_LOGGER):
            loop.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)
        assert loop_task.done(), "空闲态 cancel() 也应结束 loop"
        assert loop_task.cancelled() is False, "取消在 _loop 内被消化，任务应以正常结束收场"
        assert "打断循环" in caplog.text
        assert provider.calls == 0
        # cancel 后 loop 仍可用：send 自动重启循环
        await loop.send("你好", "next_turn")
        assert loop._task is not loop_task, "send 应重启出一个新的 loop task"
        await wait_until(lambda: records(session, "turn/end"))
        assert last(session, "turn/end").data.reason_type == "success"
        assert provider.calls == 1
    finally:
        await shutdown(loop, loop._task)


# ===========================================================================
# 正常收敛（不外抛）
# ===========================================================================


@pytest.mark.asyncio
async def test_P1_模型不再请求工具_记success():
    """单轮纯文本回复：一次调用收敛，step/turn 均 success，assistant/message 落盘"""
    loop, provider, session = make_loop([LLMResponse(content="你好", finish_reason="stop")])

    await loop.turn(user_message())

    assert provider.calls == 1
    assert len(records(session, "assistant/message")) == 1
    assert last(session, "step/end").data.reason_type == "success"
    assert last(session, "turn/end").data.reason_type == "success"


@pytest.mark.asyncio
async def test_P1b_流式chunk逐片段落盘():
    """流式片段（正文/推理/finish）逐条写入 assistant/chunk，最终产出完整 LLMResponse"""
    script = [[
        StreamChunk(content="你"),
        StreamChunk(reasoning_content="想"),
        StreamChunk(finish_reason="stop"),
        LLMResponse(content="你好", finish_reason="stop"),
    ]]
    loop, provider, session = make_loop(script)

    await loop.turn(user_message())

    chunks = records(session, "assistant/chunk")
    assert [c.data.type for c in chunks] == ["content", "reasoning", "finish"]
    assert len(records(session, "assistant/message")) == 1
    assert last(session, "turn/end").data.reason_type == "success"


@pytest.mark.asyncio
async def test_P2_工具循环正常收敛():
    """模型请求工具 → 执行成功回喂 → 模型下一轮收敛：两步均 success，调用与结果配对"""
    loop, provider, session = make_loop(
        [tool_round("c1"), LLMResponse(content="查询完成", finish_reason="stop")],
        tools=OkTool(),
    )

    await loop.turn(user_message())

    assert provider.calls == 2
    assert len(records(session, "tool/call")) == 1
    tool_results = records(session, "tool/result")
    assert len(tool_results) == 1
    assert tool_results[0].data.call_id == "c1"
    assert tool_results[0].data.is_error is False
    assert [r.data.reason_type for r in records(session, "step/end")] == ["success", "success"]
    assert last(session, "turn/end").data.reason_type == "success"


@pytest.mark.asyncio
async def test_P3_工具执行失败被工具层兜底_不上抛():
    """工具抛异常由 ToolRegister 兜成错误结果回喂（功能降级），loop 不中断，下轮收敛"""
    loop, provider, session = make_loop(
        [tool_round("c1", tool_name="flaky"), LLMResponse(content="工具坏了", finish_reason="stop")],
        tools=FlakyTool(),  # 未配置熔断
    )

    await loop.turn(user_message())

    tool_results = records(session, "tool/result")
    assert len(tool_results) == 1
    assert tool_results[0].data.is_error is True
    assert provider.calls == 2, "工具失败不应上抛，模型下一轮收敛"
    assert last(session, "turn/end").data.reason_type == "success"


@pytest.mark.asyncio
async def test_P4_参数JSON解析失败_回喂带hint结果():
    """模型输出非法 JSON：工具不执行，回喂解析错误 + 原文，loop 不中断"""
    bad_round = [LLMResponse(content=None, tool_call_requests=[
        RawToolCall(id="c_bad", name="ok", arguments='{"a":')
    ], usage=Usage())]
    loop, provider, session = make_loop(
        [bad_round, LLMResponse(content="重来", finish_reason="stop")],
        tools=OkTool(),
    )

    await loop.turn(user_message())

    tool_results = records(session, "tool/result")
    assert len(tool_results) == 1
    content = tool_results[0].data.message.content
    assert "不是合法 JSON" in content
    assert '{"a":' in content, "原文回显供模型定位错误"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_P5_用户取消_step与turn记interrupted后上抛():
    """模型调用挂起时取消 turn 任务：step/turn 先落 interrupted 终态，取消再原样上抛，
    使 task 处于 cancelled（与 asyncio 原设计对齐）"""
    loop, provider, session = make_loop(["hang"])

    task = asyncio.create_task(loop.turn(user_message("取消我")))
    await asyncio.sleep(0.01)  # 让任务进入挂起的模型调用
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled() is True, "取消原样上抛，task 应处于 cancelled"
    assert last(session, "step/end").data.reason_type == "interrupted"
    assert last(session, "turn/end").data.reason_type == "interrupted"
    assert last(session, "turn/end").data.reason_text == "用户主动打断"
    assert last(session, "turn/end").data.error_type == "CancelledError", "终态类别记异常类名"


@pytest.mark.asyncio
async def test_P6_达到step_limit_无人认领上抛记error():
    """工具一直成功但模型每轮都继续请求工具：达到 step_limit 强制终止。

    预算检查在开步之前，故超限那一轮没有 step/start、也不写 step/end；已执行的
    step_limit 步各自 success，超限经无人认领上抛（cause 为 MaxStepsExceededError），
    turn/end 记 error。
    """
    script = [tool_round(f"c{i}") for i in range(3)]
    loop, provider, session = make_loop(script, tools=OkTool(), step_limit=3)

    with pytest.raises(AgentUnclaimedError) as excinfo:
        await loop.turn(user_message())

    assert provider.calls == 3, "恰好执行 step_limit 次模型请求后强制终止"
    assert [r.data.reason_type for r in records(session, "step/end")] == ["success"] * 3
    assert isinstance(excinfo.value.__cause__, MaxStepsExceededError)
    assert last(session, "turn/end").data.reason_type == "error"
    assert "达到最大步数" in last(session, "turn/end").data.reason_text


# ===========================================================================
# 终止出口：异常上抛（step/turn 均先归一化终态）
# ===========================================================================


@pytest.mark.asyncio
async def test_P7_LLM调用超时_无人认领上抛且stepend记error():
    """等待模型超时（TimeoutError）：request/error 无人认领 → AgentUnclaimedError 上抛；
    step/end 与 turn/end 都必须记 error"""
    loop, provider, session = make_loop(["hang"])
    loop._LLM_CALL_TIMEOUT = 0.01

    with pytest.raises(AgentUnclaimedError) as excinfo:
        await loop.turn(user_message("超时"))

    assert isinstance(excinfo.value.__cause__, TimeoutError)
    assert last(session, "step/end").data.reason_type == "error"
    assert last(session, "turn/end").data.reason_type == "error"


@pytest.mark.asyncio
async def test_P8_认证失败无人认领_不等待直接终止():
    """401 认证失败属配置类错误：策略表已无对应档 → waterfall 无决策 → 抛
    AgentUnclaimedError；不重试、不退避（模型只调一次），也不落 llm/retry 记录"""
    strategy = LLMRerty()  # 强引用，保证弱引用注册有效
    script = [LLMAuthError("401 认证失败") for _ in range(3)]
    loop, provider, session = make_loop(script, step_limit=2, retry_strategy=strategy)
    waits = install_recording_delay(loop)

    with pytest.raises(AgentUnclaimedError) as excinfo:
        await loop.turn(user_message())

    assert provider.calls == 1, "配置类错误不再继续下一次调用"
    assert isinstance(excinfo.value.__cause__, LLMAuthError)
    assert waits == [], "不重试就不退避"
    assert records(session, "llm/retry") == [], "未重试就不落 llm/retry（否则虚增重试次数）"
    assert session.llm_retry_count(session.turn) == 0
    assert last(session, "turn/end").data.reason_type == "error"


@pytest.mark.asyncio
async def test_P9_backoff_retry_记记录并退避_超限上抛error():
    """429 限流属 backoff_retry：每次决定重试记一条 llm/retry 并退避；超过上限后抛
    RetryExhaustedError（cause 为限流）——耗尽那次未重试，不落记录，
    turn/end 的 reason_text 保留重试档位与原始错误"""
    strategy = LLMRerty()
    script = [LLMRateLimitError("429 限流") for _ in range(3)]
    loop, provider, session = make_loop(script, max_retry_count=2, retry_strategy=strategy)
    waits = install_recording_delay(loop)

    with pytest.raises(RetryExhaustedError) as excinfo:
        await loop.turn(user_message())

    assert provider.calls == 3, "上限 2 次重试后第 3 次失败即耗尽"
    assert isinstance(excinfo.value.__cause__, LLMRateLimitError)
    retry_records = records(session, "llm/retry")
    assert [r.data.reason for r in retry_records] == [
        "backoff_retry", "backoff_retry"
    ], "只记真正重试的两次，耗尽的第三次不落记录"
    assert [r.data.retry_count for r in retry_records] == [1, 2]
    assert session.llm_retry_count(session.turn) == 2, "llm/retry 条数 = 实际重试次数"
    assert len(waits) == 2, "两次重试各退避一次（耗尽不再等待）"
    assert [r.data.reason_type for r in records(session, "step/end")] == ["error"] * 3, \
        "每次失败的尝试各占一步，各记 error"
    assert last(session, "turn/end").data.reason_type == "error"
    assert "2/2" in last(session, "turn/end").data.reason_text
    assert "429 限流" in last(session, "turn/end").data.reason_text


@pytest.mark.asyncio
async def test_P10_LLMCallError基类_无人认领上抛():
    """来源未细分的 LLMCallError：策略注册表不匹配 → waterfall 无决策 → AgentUnclaimedError 上抛"""
    strategy = LLMRerty()
    loop, provider, session = make_loop([LLMCallError("未知来源错误")], retry_strategy=strategy)

    with pytest.raises(AgentUnclaimedError) as excinfo:
        await loop.turn(user_message())

    assert isinstance(excinfo.value.__cause__, LLMCallError)
    assert last(session, "step/end").data.reason_type == "error"
    assert last(session, "turn/end").data.reason_type == "error"


@pytest.mark.asyncio
async def test_P11_工具熔断抛错_经ExceptionGroup上抛():
    """raise_on_break 工具连续失败触发熔断抛错：经 TaskGroup 以 ExceptionGroup 上抛，
    loop 触发一次 request/error；订阅方不给出决策 → AgentUnclaimedError 上抛（cause 为 group）"""
    errors = []

    async def on_error(payload, nxt):
        errors.append(payload.error_type)
        return None  # 无人认领

    tool = FlakyTool(max_consecutive_failures=5, raise_on_break=True)
    script = [tool_round(f"c{i}", tool_name="flaky") for i in range(5)]
    loop, provider, session = make_loop(script, tools=tool, step_limit=20)
    loop._event_service.register(REQUEST_ERROR.name, on_error)

    with pytest.raises(AgentUnclaimedError) as excinfo:
        await loop.turn(user_message())

    assert len(errors) == 1, "一次错误只触发一次 request/error"
    assert isinstance(errors[0], ExceptionGroup), "熔断错经 TaskGroup 上抛，外层是 group"
    assert [type(e) for e in leaves(errors[0])] == [ToolConsecutiveFailureError]
    assert errors[0] is excinfo.value.__cause__
    assert last(session, "step/end").data.reason_type == "error"
    assert last(session, "turn/end").data.reason_type == "error"


@pytest.mark.asyncio
async def test_P12_非熔断ExceptionGroup_无人认领上抛():
    """工具执行抛出非熔断异常（经 TaskGroup 包成 ExceptionGroup）：无人认领 → 抛
    AgentUnclaimedError，cause 保留原始 group"""
    loop, provider, session = make_loop([tool_round("c1")], tools=OkTool())

    async def boom(call):
        raise RuntimeError("工具外异常")

    loop.tool_register.execute = boom

    with pytest.raises(AgentUnclaimedError) as excinfo:
        await loop.turn(user_message())

    assert [type(e) for e in leaves(excinfo.value.__cause__)] == [RuntimeError]
    assert last(session, "step/end").data.reason_type == "error"
    assert last(session, "turn/end").data.reason_type == "error"


@pytest.mark.asyncio
async def test_P13_其他未捕获异常_无人认领上抛():
    """provider 内部 bug（非 LLMCallError）穿透 step 的 except 链：step/end 记 error，
    无人认领后以 AgentUnclaimedError 上抛（cause 为原异常），turn/end 同步记 error"""
    loop, provider, session = make_loop([RuntimeError("provider boom")])

    with pytest.raises(AgentUnclaimedError) as excinfo:
        await loop.turn(user_message())

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert last(session, "step/end").data.reason_type == "error"
    assert last(session, "turn/end").data.reason_type == "error"


@pytest.mark.asyncio
async def test_P14_流中途失败_不补finish块():
    """流中途失败：provider 只在流正常结束时产出 finish 块（见 openai_provider），
    失败时该块自然缺失——session 上只剩半截 content chunk，以此区分"正常结束"与
    "调用失败"；异常经无人认领上抛"""
    script = [[StreamChunk(content="半截"), LLMCallError("连接断了")]]
    loop, provider, session = make_loop(script)

    with pytest.raises(AgentUnclaimedError) as excinfo:
        await loop.turn(user_message())

    assert isinstance(excinfo.value.__cause__, LLMCallError)
    chunks = records(session, "assistant/chunk")
    assert [c.data.finish_reason for c in chunks] == [None], "失败时没有 finish 块"
    assert last(session, "turn/end").data.reason_type == "error"


# ===========================================================================
# 记录形态：发生点记账 / 异常链文本 / step 配对 / 消息面补全
# ===========================================================================


@pytest.mark.asyncio
async def test_R1_llm_retry只记真正重试_条数即重试次数():
    """llm/retry 是"第几次重试"的记录：两次限流重试后成功 → 2 条，各带 reason /
    error_type / 异常链文本，且 llm_retry_count（loop 的 attempt 计数口径）与实际重试次数一致"""
    strategy = LLMRerty()
    script = [
        LLMRateLimitError("429 限流"),
        LLMRateLimitError("429 限流"),
        LLMResponse(content="最终回复", usage=Usage()),
    ]
    loop, provider, session = make_loop(script, max_retry_count=3, retry_strategy=strategy)
    install_recording_delay(loop)

    await loop.turn(user_message())

    assert provider.calls == 3, "初始 1 次 + 重试 2 次"
    retry_records = records(session, "llm/retry")
    assert [r.data.reason for r in retry_records] == ["backoff_retry", "backoff_retry"]
    assert [r.data.retry_count for r in retry_records] == [1, 2]
    assert all(r.data.error_type == "LLMRateLimitError" for r in retry_records)
    assert all("429 限流" in r.data.error_message for r in retry_records)
    assert session.llm_retry_count(session.turn) == 2 == len(retry_records)


def test_R2_异常链文本_逐层缩进_限深3层_展开group():
    """_format_error_chain：逐层缩进输出；超过 3 层显式标注截断（不静默丢弃）；
    ExceptionGroup 展开其成员（TaskGroup 的 group 其 __cause__ 为空，只能从 exceptions 取）"""
    deep: BaseException = RuntimeError("第4层")
    for i in (3, 2, 1):  # 逐层以 raise ... from 挂起因，构造 4 层异常链
        try:
            raise RuntimeError(f"第{i}层") from deep
        except RuntimeError as e:
            deep = e

    assert _format_error_chain(deep).splitlines() == [
        "RuntimeError: 第1层",
        "  RuntimeError: 第2层",
        "    RuntimeError: 第3层",
        "      …（更深的异常未展开）",
    ]
    assert "第4层" not in _format_error_chain(deep)

    group = ExceptionGroup("批失败", [ValueError("甲"), TypeError("乙")])
    assert _format_error_chain(group).splitlines() == [
        "ExceptionGroup: 批失败 (2 sub-exceptions)",
        "  ValueError: 甲",
        "  TypeError: 乙",
    ]


@pytest.mark.asyncio
async def test_R3_预算拒绝的那一轮不留step_end():
    """step/start 与 step/end 严格配对：超限那一轮在开步前就被拒绝，
    既不写 step/start 也不写 step/end"""
    script = [tool_round(f"c{i}") for i in range(3)]
    loop, provider, session = make_loop(script, tools=OkTool(), step_limit=2)

    with pytest.raises(AgentUnclaimedError):
        await loop.turn(user_message())

    assert len(records(session, "step/start")) == 2
    assert len(records(session, "step/end")) == 2


@pytest.mark.asyncio
async def test_R4_异常打断工具调用_补齐占位tool_result():
    """工具执行抛错时 tool/call 已落盘却没有配对结果：补一条 is_error=True 的占位
    tool/result，避免消息面出现"有 tool_calls 却没有 tool 响应"的非法组合"""
    loop, provider, session = make_loop([tool_round("c1")], tools=OkTool())

    async def boom(call):
        raise RuntimeError("工具外异常")

    loop.tool_register.execute = boom

    with pytest.raises(AgentUnclaimedError):
        await loop.turn(user_message())

    calls = records(session, "tool/call")
    results = records(session, "tool/result")
    assert [c.data.call_id for c in calls] == ["c1"]
    assert [r.data.call_id for r in results] == ["c1"]
    assert results[0].data.is_error is True
    assert "被中断" in results[0].data.message.content
    # 补齐后消息面合法：tool_calls 之后必有 role=tool 的响应
    assert session.derive_messages()[-1].role == "tool"


@pytest.mark.asyncio
async def test_R5_turnend记终态错误类别():
    """turn/end 的 error_type 承载"终止本 turn 的类别"，不是异常链里的原错误：
    成功为空串；重试耗尽记 RetryExhaustedError，原错误（429 限流）只留在 reason_text 链里"""
    strategy = LLMRerty()
    script = [
        LLMResponse(content="成功", finish_reason="stop"),
        LLMRateLimitError("429 限流"),
        LLMRateLimitError("429 限流"),
    ]
    loop, provider, session = make_loop(script, max_retry_count=1, retry_strategy=strategy)
    install_recording_delay(loop)

    await loop.turn(user_message("第一轮"))
    with pytest.raises(RetryExhaustedError):
        await loop.turn(user_message("第二轮"))

    ends = records(session, "turn/end")
    assert [e.data.reason_type for e in ends] == ["success", "error"]
    assert [e.data.error_type for e in ends] == ["", "RetryExhaustedError"]
    assert "LLMRateLimitError" in ends[1].data.reason_text, "原错误只在链文本里，不占类别"


@pytest.mark.asyncio
async def test_R6_无人认领的turnend类别人工可查():
    """无人认领时 turn/end 记 AgentUnclaimedError（触发它的原错误作为 cause 在链文本里）"""
    loop, provider, session = make_loop([RuntimeError("provider boom")])

    with pytest.raises(AgentUnclaimedError):
        await loop.turn(user_message())

    turn_end = last(session, "turn/end")
    assert turn_end.data.error_type == "AgentUnclaimedError"
    assert turn_end.data.reason_text.splitlines()[0] == "AgentUnclaimedError: 无错误处理策略的错误"
    assert "RuntimeError: provider boom" in turn_end.data.reason_text


# ===========================================================================
# 请求组装：System Prompt / System Reminder / runtime context
# ===========================================================================


@pytest.mark.asyncio
async def test_请求组装_system_prompt置首位():
    """每 step 组装的请求消息列表：第 1 位是 System Prompt（role=system），
    其后才是会话消息面；system prompt 由 request/header 承载，不落 message 记录"""
    loop, provider, session = make_loop([LLMResponse(content="好的", finish_reason="stop")])

    await loop.turn(user_message("你好"))

    seen = provider.seen_messages[0]
    assert seen[0].role == "system"
    assert "个人助手" in seen[0].content  # 全局 identity section
    assert seen[1].role == "user" and seen[1].content == "你好"
    # session 里没有 system 记录（system prompt 走 request/header）
    msg_roles = [r.data.message.role for r in session.record_list if hasattr(r.data, "message")]
    assert "system" not in msg_roles


@pytest.mark.asyncio
async def test_请求组装_system_reminder置第二位并落request_header():
    """注册 system reminder：第 2 位是 role=user 的 <system-reminder> 包裹消息，
    内容同时以 request/header 事件落盘（不新增 message 记录）"""
    loop, provider, session = make_loop([LLMResponse(content="好的", finish_reason="stop")])
    loop.system_prompt.register_system_reminder(loop.name, "请遵守编码规范")

    await loop.turn(user_message("你好"))

    seen = provider.seen_messages[0]
    assert seen[0].role == "system"
    assert seen[1].role == "user"
    assert seen[1].content == "<system-reminder>请遵守编码规范</system-reminder>"
    assert seen[2].content == "你好"
    assert session.latest_request_header().system_reminder == "请遵守编码规范"


@pytest.mark.asyncio
async def test_请求组装_无reminder不产生第二位消息():
    """未注册 system reminder：不产生多余的第 2 位消息，request/header 记空串"""
    loop, provider, session = make_loop([LLMResponse(content="好的", finish_reason="stop")])

    await loop.turn(user_message("你好"))

    seen = provider.seen_messages[0]
    assert [m.role for m in seen] == ["system", "user"]
    assert session.latest_request_header().system_reminder == ""


@pytest.mark.asyncio
async def test_请求组装_runtime_context合并进用户消息并落盘():
    """runtime context 作为追加文本块合并进当前用户消息，并随 user/message 落盘，
    保证每一步的输入都可由 session 复现"""
    loop, provider, session = make_loop([LLMResponse(content="好的", finish_reason="stop")])
    loop.system_prompt.register_context(loop.name, "当前时间: 2026-09-12")

    await loop.turn(user_message("你好"))

    user_record = records(session, "user/message")[0]
    expected = [
        {"type": "text", "text": "你好"},
        {"type": "text", "text": "当前时间: 2026-09-12"},
    ]
    assert user_record.data.message.content == expected
    # 发给模型的消息与落盘记录一致（输入即落盘）
    assert provider.seen_messages[0][-1].content == expected


@pytest.mark.asyncio
async def test_请求组装_工具循环后续step复用请求前缀():
    """工具调用循环：user/message 只写首个 step，但每个 step 都重新组装出
    System Prompt 置首的完整请求"""
    loop, provider, session = make_loop(
        [tool_round("c1"), LLMResponse(content="完成", finish_reason="stop")],
        tools=OkTool(),
    )

    await loop.turn(user_message("你好"))

    assert len(records(session, "user/message")) == 1
    assert provider.calls == 2
    for seen in provider.seen_messages:
        assert seen[0].role == "system"
    # 第二个 step 不重复发出用户消息：请求里仍只有一条 user 消息
    assert sum(1 for m in provider.seen_messages[1] if m.role == "user") == 1


@pytest.mark.asyncio
async def test_请求组装_自定义提示词与落盘还原(tmp_path):
    """自定义提示词段 + system reminder + runtime context：组装正确，
    且落盘还原后能重建出与请求一致的消息列表（每一步输入可复现）"""
    event_service = EventService()
    project_path = tmp_path / "proj"
    store = SessionStore(tmp_path / "sessions", event_service)
    session = store.create("请求组装落盘", project_path)

    system_prompt = SystemPrompt()
    system_prompt.register_section("coder", PrompSection(name="tool_guide", order=10, text="你可以使用工具查询天气。"))
    system_prompt.register_system_reminder("coder", "请遵守编码规范")
    system_prompt.register_context("coder", "当前时间: 2026-09-12")

    provider = ScriptedProvider([LLMResponse(content="好的", finish_reason="stop")])
    config = AgentConfig(step_limit=10, max_retry_count=2)
    loop = ReActAgentLoop(event_service, session, system_prompt, config, provider,
                          name="coder", prompt_render_parame={})

    await loop.turn(user_message("你好"))
    session.presistence.presist()

    # 组装正确：第 1 位 system（全局 + 自定义段按 order 有序），第 2 位 reminder，第 3 位用户消息
    seen = provider.seen_messages[0]
    assert seen[0].role == "system"
    assert "个人助手" in seen[0].content  # 全局 identity（order=-100）
    assert "你可以使用工具查询天气。" in seen[0].content  # 自定义段（order=10）
    assert seen[0].content.index("个人助手") < seen[0].content.index("你可以使用工具查询天气。")
    assert seen[1].content == "<system-reminder>请遵守编码规范</system-reminder>"
    assert seen[2].content == [
        {"type": "text", "text": "你好"},
        {"type": "text", "text": "当前时间: 2026-09-12"},
    ]

    # 落盘还原：system prompt / reminder 由 request/header 承载，runtime 由 user/message 承载
    restored = store.load(session.meta_data.session_id, project_path)
    header_mem, header_disk = session.latest_request_header(), restored.latest_request_header()
    assert header_disk.system_prompt == header_mem.system_prompt
    assert header_disk.system_reminder == "请遵守编码规范"
    assert restored.derive_messages() == session.derive_messages()

    # 由落盘内容重建完整请求，应与实际发出的逐条一致（其后多出的 assistant 为模型回复）
    rebuilt = [
        Message(role="system", content=header_disk.system_prompt),
        Message(role="user", content=f"<system-reminder>{header_disk.system_reminder}</system-reminder>"),
    ]
    rebuilt.extend(restored.derive_messages())
    assert rebuilt[-1].role == "assistant"
    assert [m.role for m in rebuilt[:len(seen)]] == [m.role for m in seen]
    assert [m.content for m in rebuilt[:len(seen)]] == [m.content for m in seen]


@pytest.mark.asyncio
async def test_请求组装_提示词变化写change快照():
    """更换提示词段：下一轮组装结果变化，写 reason=change 的 request/header 快照，
    且新一轮请求以最新 system prompt 置首"""
    loop, provider, session = make_loop([
        LLMResponse(content="回复一", finish_reason="stop"),
        LLMResponse(content="回复二", finish_reason="stop"),
    ])

    await loop.turn(user_message("第一轮"))
    headers = records(session, "request/header")
    assert len(headers) == 1 and headers[0].data.reason == "initial"

    # 遮蔽全局 identity：换一段提示词，组装结果随之变化
    loop.system_prompt.register_section(loop.name, PrompSection(name="identity", order=-100, text="你是新助手"))
    await loop.turn(user_message("第二轮"))

    headers = records(session, "request/header")
    assert len(headers) == 2
    assert headers[1].data.reason == "change"
    assert "你是新助手" in headers[1].data.system_prompt
    assert "你是新助手" in provider.seen_messages[1][0].content


@pytest.mark.asyncio
async def test_请求组装_多轮对话用户消息不重复发出():
    """两轮对话：每轮各写一条 user/message，历史里的旧用户消息不重复发出"""
    loop, provider, session = make_loop([
        LLMResponse(content="回复一", finish_reason="stop"),
        LLMResponse(content="回复二", finish_reason="stop"),
    ])

    await loop.turn(user_message("第一轮"))
    await loop.turn(user_message("第二轮"))

    assert [r.data.message.content for r in records(session, "user/message")] == ["第一轮", "第二轮"]

    second_seen = provider.seen_messages[1]
    # system 置首；历史按 第一轮 → 回复一 → 第二轮 只出现一次
    assert [m.role for m in second_seen] == ["system", "user", "assistant", "user"]
    assert sum(1 for m in second_seen if m.content == "第一轮") == 1
    assert sum(1 for m in second_seen if m.content == "第二轮") == 1
