"""OpenAIProvider 的 mock 数据测试（不发起真实网络请求）。

覆盖：
O1  stream_chat：mock 流式 chunk 聚合（正文 / 推理 / 工具调用碎片 / usage / finish）
O2  _classify_openai_error：SDK 异常按状态码与响应体关键词映射到 execption 分类树
O3  stream_chat：SDK 异常翻译后抛出（调用方按 LLMCallError 分类接住）
O4  _retry_after：从响应头提取服务端建议的重试等待秒数

现有 test_openai_provider.py 覆盖 chat() 的非流式解析与 length 截断标记，本文件不重复。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from myagent.agent.core.provider import LLMResponse, Message, Usage
from myagent.agent.execption import (
    LLMAuthError,
    LLMCallError,
    LLMConnectionError,
    LLMContextExceededError,
    LLMModelError,
    LLMQuotaError,
    LLMRateLimitError,
)
from myagent.agent.llm.openai_provider import (
    OpenAIProvider,
    _classify_openai_error,
    _retry_after,
)


# ---------------------------------------------------------------------------
# mock 数据构造
# ---------------------------------------------------------------------------


class FakeStream:
    """把一组预置 chunk 当作 SDK 的异步流返回（供 stream_chat 的 async for 消费）。"""

    def __init__(self, chunks: list):
        self._chunks = chunks

    def __aiter__(self):
        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()


def _delta(content=None, reasoning=None, tool_calls=None):
    return SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)


def _chunk(delta=None, finish_reason=None, usage=None):
    """造一个长得像 SDK ChatCompletionChunk 的假片段（字段与 stream_chat 读取一致）。"""
    choices = [] if delta is None else [SimpleNamespace(delta=delta, finish_reason=finish_reason)]
    return SimpleNamespace(choices=choices, usage=usage)


def _request():
    return httpx.Request("POST", "https://test.local/v1/chat/completions")


def _status_error(cls, status: int, body: dict):
    """构造带 HTTP 状态的 SDK 异常（状态码与响应体是分类依据）。"""
    response = httpx.Response(status, request=_request(), json=body)
    return cls("sdk 错误", response=response, body=body)


def make_provider() -> OpenAIProvider:
    provider = OpenAIProvider("test-model", api_key="test-key", base_url="https://test.local/v1")
    provider._client = AsyncMock()  # 真客户端换成替身，不会发请求
    return provider


# ---------------------------------------------------------------------------
# O1 流式聚合
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_O1_stream_chat_聚合正文推理工具调用与usage():
    """mock 流式片段：正文/推理增量拼接、工具调用碎片按槽位拼接、usage 与 finish_reason 归位"""
    provider = make_provider()
    tool_frag_1 = SimpleNamespace(
        index=0, id="call_1",
        function=SimpleNamespace(name="get_weather", arguments='{"date"'),
    )
    tool_frag_2 = SimpleNamespace(
        index=0, id=None,
        function=SimpleNamespace(name=None, arguments=': "2026-09-05"}'),
    )
    stream_chunks = [
        _chunk(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3)),
        _chunk(delta=_delta(content="今天")),
        _chunk(delta=_delta(reasoning="先思考一下")),
        _chunk(delta=_delta(tool_calls=[tool_frag_1])),
        _chunk(delta=_delta(tool_calls=[tool_frag_2])),
        _chunk(delta=_delta(), finish_reason="tool_calls"),
    ]
    provider._client.chat.completions.create = AsyncMock(return_value=FakeStream(stream_chunks))

    items = [item async for item in provider.stream_chat([Message(role="user", content="hi")])]

    final = items[-1]
    assert isinstance(final, LLMResponse)
    assert final.content == "今天"
    assert final.reasoning_content == "先思考一下"
    assert final.finish_reason == "tool_calls"
    assert final.usage == Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    assert len(final.tool_call_requests) == 1
    call = final.tool_call_requests[0]
    assert call.id == "call_1" and call.name == "get_weather"
    assert call.arguments == '{"date": "2026-09-05"}', "工具参数碎片应按槽位拼接为 wire 字符串"
    assert call.truncated is False
    # finish 块在最终 LLMResponse 之前单独产出（用于区分正常结束与中途失败）
    assert any(getattr(i, "finish_reason", None) == "tool_calls" for i in items[:-1])


# ---------------------------------------------------------------------------
# O2 错误分类映射
# ---------------------------------------------------------------------------


CLASSIFY_CASES = [
    (
        "连接层",
        lambda: openai.APIConnectionError(request=_request()),
        LLMConnectionError,
    ),
    (
        "429限流",
        lambda: _status_error(openai.RateLimitError, 429, {"error": {"code": "rate_limit", "message": "too many requests"}}),
        LLMRateLimitError,
    ),
    (
        "401认证",
        lambda: _status_error(openai.AuthenticationError, 401, {"error": {"code": "invalid_api_key", "message": "bad key"}}),
        LLMAuthError,
    ),
    (
        "403权限",
        lambda: _status_error(openai.PermissionDeniedError, 403, {"error": {"code": "forbidden", "message": "no permission"}}),
        LLMAuthError,
    ),
    (
        "404模型不存在",
        lambda: _status_error(openai.NotFoundError, 404, {"error": {"code": "model_not_found", "message": "no such model"}}),
        LLMModelError,
    ),
    (
        "400上下文超长",
        lambda: _status_error(openai.BadRequestError, 400, {"error": {"code": "context_length_exceeded", "message": "too long"}}),
        LLMContextExceededError,
    ),
    (
        "配额不足_关键词优先于403",
        lambda: _status_error(openai.PermissionDeniedError, 403, {"error": {"code": "insufficient_quota", "message": "余额不足"}}),
        LLMQuotaError,
    ),
    (
        "5xx服务端过载",
        lambda: _status_error(openai.InternalServerError, 500, {"error": {"code": "server_busy", "message": "busy"}}),
        LLMRateLimitError,
    ),
    (
        "未细分兜底",
        lambda: openai.APIError("未分类错误", request=_request(), body=None),
        LLMCallError,
    ),
]


@pytest.mark.parametrize(
    "factory, expected",
    [(c[1], c[2]) for c in CLASSIFY_CASES],
    ids=[c[0] for c in CLASSIFY_CASES],
)
def test_O2_SDK异常分类映射(factory, expected):
    """状态码 + 响应体关键词联合判定错误来源分类（429/403 语义随供应商不同，不能只看码）"""
    error = factory()

    classified = _classify_openai_error(error)

    assert isinstance(classified, expected)
    assert isinstance(classified, LLMCallError), "分类结果必须落在 LLMCallError 分类树内"
    assert classified.__cause__ is error, "保留原始 SDK 异常便于排查"
    assert classified.details.get("http_status") == getattr(error, "status_code", None)


# ---------------------------------------------------------------------------
# O3 异常翻译后抛出
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_O3_stream_chat_SDK异常翻译后抛出():
    """流式调用中 SDK 抛错：翻译为分类树子类后抛出（loop 侧按 LLMCallError 接住走重试决策）"""
    provider = make_provider()
    provider._client.chat.completions.create = AsyncMock(
        side_effect=_status_error(
            openai.RateLimitError, 429,
            {"error": {"code": "rate_limit", "message": "too many requests"}},
        )
    )

    with pytest.raises(LLMRateLimitError):
        async for _ in provider.stream_chat([Message(role="user", content="hi")]):
            pass


# ---------------------------------------------------------------------------
# O4 retry-after 提取
# ---------------------------------------------------------------------------


def test_O4_retry_after_从响应头提取():
    """服务端建议的重试等待秒数：两种头名均可识别，非法值与缺失头返回 None"""
    assert _retry_after(SimpleNamespace(headers={"retry-after": "3"})) == 3.0
    assert _retry_after(SimpleNamespace(headers={"Retry-After": "2.5"})) == 2.5
    assert _retry_after(SimpleNamespace(headers={"retry-after": "abc"})) is None
    assert _retry_after(SimpleNamespace(headers={})) is None
    assert _retry_after(SimpleNamespace()) is None

