import pytest
from unittest.mock import AsyncMock
from myagent.agent.core.provider import ToolCallRequest, Usage
from myagent.agent.llm.openai_provider import OpenAIProvider,LLMCallError
from myagent.agent.core.provider import Message
from types import SimpleNamespace
import openai
@pytest.fixture
def provider():
    p = OpenAIProvider("test-model", api_key="test-key", base_url="https://test.local/v1")  # key 是假的，反正不会真发请求
    p._client = AsyncMock()      # 把真客户端换成替身
    return p

def make_fake_completions(content="ok", finish_reason="stop", reasoning_content=None, tool_calls=None):
    """造一个长得像 SDK ChatCompletion 的假响应，字段对应 chat() 和 parse_tool_call() 实际读取的属性"""
    message = SimpleNamespace(content=content, reasoning_content=reasoning_content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    return SimpleNamespace(choices=[choice], usage=usage)

@pytest.mark.asyncio
async def test_openai_provider_response(provider):
    reasoning_content = "测试"
    tool_calls = [
        SimpleNamespace(id = "call_xxx",function = SimpleNamespace(name = "test",arguments = '{"test":"a" }'))
    ]
    provider._client.chat.completions.create.return_value = make_fake_completions(finish_reason="tool_calls",reasoning_content =reasoning_content,tool_calls=tool_calls )
    response = await provider.chat([Message(role="user",content="测试")])

    assert response.content == "ok" , "输出返回错误"
    assert response.reasoning_content == "测试" ,  "输出返回错误"
    assert response.tool_call_requests == [ToolCallRequest(id= "call_xxx",name = "test",arguments ={"test":"a"})],"输出返回错误"
    assert response.usage == Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3),"输出返回错误"
    assert response.finish_reason == "tool_calls","输出返回错误"

    provider._client.chat.completions.create.side_effect  = LLMCallError()
    with pytest.raises(LLMCallError):
        await provider.chat([Message(role="user",content="测试")])
