from myagent.agent.core.loop import Loop,EventService,ToolRegister,Session,Message,LLMResponse
from myagent.agent.core.provider import ToolCallRequest
from myagent.agent.core.tool import Tool
from myagent.agent.llm.openai_provider import OpenAIProvider
from unittest.mock import AsyncMock
from types import SimpleNamespace
from typing import Any
import pytest

@pytest.fixture
def loop():
    tool_register = ToolRegister()
    tool_register.register(WeatherTool())
    return Loop(
        event_service=EventService(),
        tool_register=tool_register,
        session = Session(),
        llm_client= AsyncMock()
    )
    
def make_fake_chat_response_without_tool_call():
    response = SimpleNamespace(
        content = "晴天",
        reasoning_content = "测试推理过程",
        finish_reason = "stop",
        tool_call_requests = [],
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    )
    return response

def make_fake_chat_response_with_tool_call():
    response = SimpleNamespace(
        content = "",
        reasoning_content = "测试推理过程",
        finish_reason = "tool_calls",
        tool_call_requests = [ToolCallRequest(
            id = "call_xxx",name = "get_weather",arguments={"date":"2026-09-02"}
        )],
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    )
    return response
class WeatherTool(Tool):
    def __init__(self):
        super().__init__()

    @property
    def name(self) -> str:
        return "get_weather"

    @property
    def description(self) -> str:
        return "获取某天的天气状况"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "需要查询的日期，格式YYYY-MM-DD"
                }
            }
        }

    async def execute(self, **kwargs) -> str:
        date = kwargs.get("date", None)
        return f"{date}的天气是晴天"


@pytest.mark.asyncio
async def test_loop_followup(loop:Loop):
    """
    1. 测试followup的正常运行（llm输出正常，工具响应正常）
    """
    loop._llm_client.chat.side_effect = [make_fake_chat_response_with_tool_call(),make_fake_chat_response_without_tool_call()]
    response = await loop.followup(Message("user","今天天气怎么样"))
    assert len(loop._session.messages) == 4,"错误"
    # Message 的 timestamp 是创建时自动生成的，整列表 == 会因时间戳不同而失败，所以逐字段断言
    msgs = loop._session.messages
    assert msgs[0].role == "user"
    assert msgs[0].content == "今天天气怎么样"

    assert msgs[1].role == "assistant"
    assert msgs[1].content == ""
    assert msgs[1].reasoning_content == "测试推理过程"
    assert msgs[1].tool_calls == [ToolCallRequest(id="call_xxx", name="get_weather", arguments={"date": "2026-09-02"})]

    assert msgs[2].role == "tool"
    assert msgs[2].tool_call_id == "call_xxx"
    assert msgs[2].content == "2026-09-02的天气是晴天"

    assert msgs[3].role == "assistant"
    assert msgs[3].content == "晴天"
    assert msgs[3].reasoning_content == "测试推理过程"
    assert msgs[3].tool_calls == []


    