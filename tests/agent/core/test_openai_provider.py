import pytest
from unittest.mock import AsyncMock
from myagent.agent.core.provider import RawToolCall, Usage
from myagent.agent.llm.openai_provider import OpenAIProvider,LLMCallError
from myagent.agent.llm.openai_provider import LLMConnectionError
from myagent.agent.core.provider import Message
from types import SimpleNamespace
import httpx
import openai
@pytest.fixture
def provider():
    p = OpenAIProvider("test-model", api_key="test-key", base_url="https://test.local/v1")  # key 是假的，反正不会真发请求
    p._client = AsyncMock()      # 把真客户端换成替身
    return p

def make_fake_completions(content="ok", finish_reason="stop", reasoning_content=None, tool_calls=None):
    """造一个长得像 SDK ChatCompletion 的假响应，字段对应 chat() 和 extract_tool_calls() 实际读取的属性"""
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
    assert response.tool_call_requests == [RawToolCall(id= "call_xxx",name = "test",arguments = '{"test":"a" }')],"输出返回错误"
    assert response.usage == Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3),"输出返回错误"
    assert response.finish_reason == "tool_calls","输出返回错误"

    provider._client.chat.completions.create.side_effect  = LLMCallError()
    with pytest.raises(LLMCallError):
        await provider.chat([Message(role="user",content="测试")])

@pytest.mark.asyncio
async def test_length截断的tool_call提取时带truncated标记(provider):
    """finish_reason=length：提取出的 RawToolCall 打 truncated 标记（执行语境，
    供工具层选择解析失败的回喂话术）；正常 tool_calls 响应不带标记"""
    tool_calls = [
        SimpleNamespace(id="call_trunc", function=SimpleNamespace(name="test", arguments='{"test": "a'))
    ]
    provider._client.chat.completions.create.return_value = make_fake_completions(
        finish_reason="length", tool_calls=tool_calls)
    response = await provider.chat([Message(role="user",content="测试")])

    assert response.finish_reason == "length"
    assert len(response.tool_call_requests) == 1
    assert response.tool_call_requests[0].truncated is True, "length 截断的调用应带 truncated 标记"

    # 对照：正常 tool_calls 响应不带标记
    tool_calls_ok = [
        SimpleNamespace(id="call_ok", function=SimpleNamespace(name="test", arguments='{"a": 1}'))
    ]
    provider._client.chat.completions.create.return_value = make_fake_completions(
        finish_reason="tool_calls", tool_calls=tool_calls_ok)
    response_ok = await provider.chat([Message(role="user",content="测试")])

    assert response_ok.tool_call_requests[0].truncated is False


# ---------------- httpx 传输层异常 → 连接类（LLMConnectionError） ----------------
# 背景：流式迭代里冒出的 httpx 异常不会被打包成 openai.APIError，原先会裸冒泡出 provider，
# 既污染 agent 内核，又因类型未登记导致重试策略无人认领（直接判失败）。


@pytest.mark.asyncio
async def test_非流式传输层异常归一为连接类(provider):
    provider._client.chat.completions.create.side_effect = httpx.ConnectError("连不上")
    with pytest.raises(LLMConnectionError) as exc:
        await provider.chat([Message(role="user", content="测试")])

    assert isinstance(exc.value.__cause__, httpx.ConnectError), "应保留原始异常在 __cause__"


@pytest.mark.asyncio
async def test_流式传输层异常归一为连接类(provider):
    """本次线上真实故障类型：流中途对端断连（incomplete chunked read）"""

    async def boom():
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body (incomplete chunked read)"
        )
        yield  # 仅为构成 async generator

    provider._client.chat.completions.create.return_value = boom()
    with pytest.raises(LLMConnectionError) as exc:
        async for _ in provider.stream_chat([Message(role="user", content="测试")]):
            pass

    assert isinstance(exc.value.__cause__, httpx.RemoteProtocolError)
    assert "RemoteProtocolError" in str(exc.value), "异常文本应保留原始类型名，便于排查"


@pytest.mark.asyncio
async def test_超时也归为连接类(provider):
    """httpx 超时同属传输层（连接类来源：上游瞬时不可达）"""
    provider._client.chat.completions.create.side_effect = httpx.ReadTimeout("读超时")
    with pytest.raises(LLMConnectionError):
        await provider.chat([Message(role="user", content="测试")])


@pytest.mark.asyncio
async def test_HTTP状态错误不被误判为连接类(provider):
    """HTTPStatusError 不属于传输层，不能归一成"连接问题"（4xx/5xx 走状态码分类路径）"""
    request = httpx.Request("POST", "https://test.local/v1")
    provider._client.chat.completions.create.side_effect = httpx.HTTPStatusError(
        "500", request=request, response=httpx.Response(500, request=request)
    )
    with pytest.raises(httpx.HTTPStatusError):
        await provider.chat([Message(role="user", content="测试")])


@pytest.mark.asyncio
async def test_连接类错误进入退避重试档():
    """闭环：归一后的 LLMConnectionError 被重试策略识别为 backoff_retry（延迟重试）"""
    from myagent.agent.llm.llm_retry import LLMRerty
    from myagent.infra.events.payload import RequestErrorPayLoad

    async def _next():
        return "NEXT"

    decision = await LLMRerty().request_error_event(
        RequestErrorPayLoad(error_type=LLMConnectionError("RemoteProtocolError: peer closed")),
        _next,
    )
    assert decision["decision"] == "backoff_retry"
