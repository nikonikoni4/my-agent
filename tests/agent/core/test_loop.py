"""AgentLoop 对话循环的行为测试。

规格要点（来自 loop.py 的签名与事件注释）：
1. followup(system_prompt, message)：system_prompt 必须原样写入本轮的 request/header 记录
2. 无工具调用：调一次 LLM 即结束，追加 turn/end 后返回该轮回复
3. 有工具调用：执行工具并记录 tool/call、tool/result 后进入下一步，
   直到模型某次回复不再请求工具才结束
4. 流式片段（assistant/chunk）只做事件记录不进可见面，完整回复
   assistant/message 记录 source_event_seqs 指向它消费的片段
"""

import pytest

from myagent.agent.core.agent.deprecated_loop import AgentLoop
from myagent.agent.core.provider import (
    LLMProvider,
    LLMResponse,
    Message,
    StreamChunk,
    RawToolCall,
    Usage,
)
from myagent.agent.core.session.session import Session
from myagent.agent.core.session.types import SessionMetaData
from myagent.agent.core.tool.tool import Tool
from myagent.agent.core.tool.register import ToolRegister
from myagent.infra.events.service import EventService

SYSTEM_PROMPT = "你是测试助手，回答要简短"


class WeatherTool(Tool):
    """测试用工具：查某天的天气，execute 返回固定文案。"""

    @property
    def name(self) -> str:
        return "get_weather"

    @property
    def description(self) -> str:
        return "获取某天的天气状况"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "需要查询的日期，格式YYYY-MM-DD"},
            },
        }

    async def execute(self, **kwargs) -> str:
        date = kwargs.get("date", None)
        return f"{date}的天气是晴天"


class FakeProvider(LLMProvider):
    """按脚本逐轮返回结果的假 Provider。

    stream_chat 每次被调用时弹出脚本中的一轮，逐个 yield 该轮的 item
    （StreamChunk 片段或最终 LLMResponse），与 OpenAIProvider 的流式契约一致。
    """

    def __init__(self, rounds: list[list]):
        super().__init__(model="fake-model")
        self._rounds = list(rounds)
        self.calls: list[list[Message]] = []

    async def chat(self, messages, tools=None):  # 循环只走 stream_chat，这里不实现
        raise NotImplementedError

    async def stream_chat(self, messages, tools=None):
        self.calls.append(list(messages))
        if not self._rounds:
            raise RuntimeError("FakeProvider 脚本耗尽，loop 仍在继续调模型")
        for item in self._rounds.pop(0):
            yield item


class NoopPersistence:
    """占位持久化：presist 静默成功，避免 loop.persist_now 触碰真实文件。"""

    def presist(self):
        pass


def make_session() -> Session:
    session = Session(EventService(), SessionMetaData(cwd="."))
    session.presistence = NoopPersistence()
    return session


def make_loop(session: Session, rounds: list[list], *tools: Tool):
    """组装被测 AgentLoop：空事件服务 + 会话 + 工具注册表 + 假 Provider。

    Returns:
        (loop, provider)：provider 用来读取每次调 LLM 收到的消息列表。
    """
    register = ToolRegister()
    if tools:
        register.register(list(tools))
    provider = FakeProvider(rounds)
    loop = AgentLoop(EventService(), session, register, provider)
    return loop, provider


def usage() -> Usage:
    return Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3)


def find_record(session: Session, type_: str):
    return next(r for r in session.record_list if r.type == type_)


# ---------------------------------------------------------------------------
# system_prompt 落进 request/header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_prompt写入request_header_并原样返回():
    """测试场景：followup 传入的 system_prompt 原样记录在 request/header"""
    session = make_session()
    loop, provider = make_loop(session, [[LLMResponse(content="晴天", usage=usage())]])

    response = await loop.followup(SYSTEM_PROMPT, Message("user", "今天天气怎么样"))

    header = find_record(session, "request/header")
    assert header.data.system_prompt == SYSTEM_PROMPT
    assert header.data.model_name == "fake-model"
    assert header.data.reason == "initial"
    assert header.data.tools == []
    assert header.data.params is None
    # 收到的消息里只有用户消息，system prompt 不进 LLM 对话列表（走动态编排）
    assert provider.calls == [[Message("user", "今天天气怎么样")]]
    assert response.content == "晴天"


@pytest.mark.asyncio
async def test_request_header携带当前工具schema():
    """测试场景：request/header 的工具清单与工具注册表当前状态一致"""
    session = make_session()
    tool = WeatherTool()
    loop, _ = make_loop(session, [[LLMResponse(content="晴天")]], tool)

    await loop.followup(SYSTEM_PROMPT, Message("user", "查天气"))

    header = find_record(session, "request/header")
    assert header.data.tools == [tool.to_schema()]


# ---------------------------------------------------------------------------
# 主路径：无工具调用
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_无工具调用一轮结束_追加turn_end并返回回复():
    """测试场景：模型不再请求工具，一轮即结束，记录序列完整"""
    session = make_session()
    final = LLMResponse(content="晴天", reasoning_content="先查日期", usage=usage())
    loop, _ = make_loop(session, [[final]])

    response = await loop.followup(SYSTEM_PROMPT, Message("user", "今天天气怎么样"))

    assert response is final
    assert [r.type for r in session.record_list] == [
        "turn/start",
        "step/start",
        "request/header",
        "user/message",
        "assistant/message",
        "turn/end",
    ]
    turn_end = find_record(session, "turn/end")
    assert turn_end.data.reason == "success"
    # 可见面 = user/message + assistant/message，可直接推导出 LLM 输入
    assert [m.role for m in session.derive_messages()] == ["user", "assistant"]
    assert session.derive_messages()[-1].content == "晴天"


@pytest.mark.asyncio
async def test_流式片段记录为assistant_chunk_且assistant_message声明来源():
    """测试场景：content 分片流式返回，chunk 只留事件记录，最终消息声明消费的片段 seq"""
    session = make_session()
    loop, _ = make_loop(
        session,
        [[StreamChunk(content="晴"), StreamChunk(content="天"), LLMResponse(content="晴天", usage=usage())]],
    )

    response = await loop.followup(SYSTEM_PROMPT, Message("user", "今天天气怎么样"))

    assert response.content == "晴天"
    types = [r.type for r in session.record_list]
    assert types == [
        "turn/start",
        "step/start",
        "request/header",
        "user/message",
        "assistant/chunk",
        "assistant/chunk",
        "assistant/message",
        "turn/end",
    ]
    # assistant/message 声明它消费的两个片段 seq，chunk 本身不进可见面
    assistant_msg = find_record(session, "assistant/message")
    chunk_seqs = [r.seq for r in session.record_list if r.type == "assistant/chunk"]
    assert sorted(assistant_msg.source_event_seqs) == chunk_seqs
    assert [m.role for m in session.derive_messages()] == ["user", "assistant"]
    assert session.derive_messages()[-1].content == "晴天"


# ---------------------------------------------------------------------------
# 主路径：有工具调用
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_工具调用后进入下一步_直到模型不再请求工具():
    """测试场景：先发起工具调用并记录结果，再走一步得到最终回复"""
    session = make_session()
    tool_call = RawToolCall(id="call_1", name="get_weather", arguments='{"date": "2026-09-02"}')
    rounds = [
        [LLMResponse(content=None, tool_call_requests=[tool_call], usage=usage())],
        [LLMResponse(content="2026-09-02的天气是晴天", usage=usage())],
    ]
    loop, provider = make_loop(session, rounds, WeatherTool())

    response = await loop.followup(SYSTEM_PROMPT, Message("user", "查 2026-09-02 天气"))

    assert response.content == "2026-09-02的天气是晴天"
    assert [r.type for r in session.record_list] == [
        "turn/start",
        "step/start",
        "request/header",
        "user/message",
        "assistant/message",  # 发起工具调用的回复
        "tool/call",
        "tool/result",
        "step/end",
        "step/start",  # 第二步
        "assistant/message",  # 最终回复
        "turn/end",
    ]
    # 两次 LLM 调用，第二次的输入 = user + 发起调用 + tool 结果（最终回复此时尚未产生）
    assert len(provider.calls) == 2
    second_messages = provider.calls[1]
    assert [m.role for m in second_messages] == ["user", "assistant", "tool"]
    # tool 结果消息内容与配对关系
    tool_result = second_messages[2]
    assert tool_result.role == "tool"
    assert tool_result.tool_call_id == "call_1"
    assert tool_result.content == "2026-09-02的天气是晴天"
    # 记录的 tool/call 携带模型给的参数（wire 原样 JSON 字符串，解析在工具层）
    tool_call_record = find_record(session, "tool/call")
    assert tool_call_record.data.tool_name == "get_weather"
    assert tool_call_record.data.arguments == '{"date": "2026-09-02"}'
    # 可见面 = user + 发起调用 + tool 结果 + 最终回复
    assert [m.role for m in session.derive_messages()] == ["user", "assistant", "tool", "assistant"]
