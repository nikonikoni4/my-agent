"""OpenAI 兼容格式的 LLM 实现。

任何提供 chat.completions 兼容接口的供应商都可用，
火山方舟只需 base_url 传入 https://ark.cn-beijing.volces.com/api/v3。
"""
from openai import AsyncOpenAI
import openai
import json
from myagent.agent.core.provider import ChatParams, LLMProvider, LLMResponse, Message, StreamChunk, ToolCallRequest, Usage

import logging
from myagent.agent.execption import (
    LLMCallError,
    LLMAuthError,
    LLMModelError,
    LLMRateLimitError,
    LLMQuotaError,
    LLMConnectionError,
    LLMContextExceededError,
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _retry_after(e) -> float | None:
    """从响应头提取服务端建议的重试等待秒数，拿不到返回 None。"""
    headers = getattr(e, "headers", None) or {}
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _classify_openai_error(e: openai.APIError) -> LLMCallError:
    """把 openai SDK 异常翻译成 execption.py 分类树中的具体子类。

    分类依据 = 状态码 + 响应体 error.code/error.type/error.message 三者联合，
    因为 429/403 在不同供应商下语义可能不同（限流 vs 欠费），不能只看 HTTP 码。
    details 里透传原始信号，供上层归因与策略注册表判定重试。
    """
    status = getattr(e, "status_code", None)
    body = getattr(e, "body", None)
    # 兼容两种响应体：openai 标准 {"error": {...}} 与方舟扁平 {"code":...,"message":...}
    err = {}
    if isinstance(body, dict):
        err = body.get("error", body)
        if not isinstance(err, dict):
            err = body
    err_code = err.get("code") or err.get("type")
    err_msg = err.get("message") or str(e)
    text = f"{err_code or ''} {str(err_msg or '')}"

    # 连接层（无 HTTP 状态码）：断网/超时/瞬时不可达
    if isinstance(e, openai.APIConnectionError) or isinstance(e, openai.APITimeoutError):
        cls = LLMConnectionError
    # 配额/余额：各方欠费表现不一致，靠 code+message 关键词兜底（优先于 403/429 判断）
    elif any(k in text for k in ("quota", "insufficient", "balance", "欠费", "余额")):
        cls = LLMQuotaError
    # 上下文超长：400 context_length_exceeded
    elif status == 400 or any(k in text for k in ("context_length", "too long")):
        cls = LLMContextExceededError
    # 认证 401 / 权限 403
    elif status == 401 or isinstance(e, openai.AuthenticationError):
        cls = LLMAuthError
    elif status == 403 or isinstance(e, openai.PermissionDeniedError):
        cls = LLMAuthError
    # 模型/接入点不存在 404
    elif status == 404 or isinstance(e, openai.NotFoundError):
        cls = LLMModelError
    # 限流 429（且已被 quota 分支过滤过，走到这说明是纯限流）
    elif status == 429 or isinstance(e, openai.RateLimitError):
        cls = LLMRateLimitError
    # 服务端繁忙/过载：视作瞬时错误统一延迟重试
    elif status and status >= 500:
        cls = LLMRateLimitError
    # 其余 4xx（HTTP 状态码缓存异常如 400 通用错误）走基类兜底
    else:
        cls = LLMCallError

    return cls(
        f"LLM 调用错误：{err_msg}",
        details={
            "http_status": status,
            "sdk_type": type(e).__name__,
            "error_code": err_code,
            "retry_after": _retry_after(e),
        },
        cause=e,
    )


class OpenAIProvider(LLMProvider):
    """基于 openai SDK 的实现，model 和 base_url 由调用方指定"""

    def __init__(self, model: str, api_key: str, base_url: str  ,chat_params:ChatParams|None = None):
        """
        Args:
            model: 模型名或接入点 ID（如火山方舟的 ep-xxx）
            api_key: 供应商的 API Key
            base_url: 兼容接口地址，None 表示使用 OpenAI 官方地址
        """
        super().__init__(model,chat_params)
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def chat(self, messages: list[Message], tools : list[dict] | None = None) -> LLMResponse:
        """发送消息列表，返回模型回复

        Args:
            messages: 完整的对话消息列表，按时间顺序排列
            self._params: 采样参数，None 表示全部使用供应商默认值

        Returns:
            LLMResponse: 模型回复及结束原因、token 用量
        """

        try:
            kwargs: dict = {
                "model": self._model,
                "messages": [m.to_dict() for m in messages],
            }
            if self._params is not None:
                for name in ("temperature", "top_p", "max_tokens"):
                    value = getattr(self._params, name)
                    if value is not None:
                        kwargs[name] = value
                # top_k 不在 OpenAI 标准参数里，通过 extra_body 传给兼容的供应商
                if self._params.top_k is not None:
                    kwargs["extra_body"] = {"top_k": self._params.top_k}
            if tools is not None:
                kwargs["tools"] = tools
            completion = await self._client.chat.completions.create(**kwargs)

            choice = completion.choices[0]
            usage = Usage()
            if completion.usage is not None:
                usage = Usage(
                    prompt_tokens=completion.usage.prompt_tokens,
                    completion_tokens=completion.usage.completion_tokens,
                    total_tokens=completion.usage.total_tokens,
                )

            # 工具调用解析
            if choice.finish_reason == 'tool_calls':
                tool_call_requests = self.parse_tool_call(choice.message)
            elif choice.finish_reason == 'length':
                # max_tokens 截断：截断响应可能夹带参数不完整的工具调用，交给检查函数判定
                raw_calls = [
                    {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments or ""}
                    for tc in (choice.message.tool_calls or [])
                ]
                tool_call_requests = self.check_truncated_tool_calls(choice.finish_reason, raw_calls)
            else:
                tool_call_requests = None
        except openai.APIError as e :
            logger.debug(f"llm call 错误 {e}")
            raise _classify_openai_error(e) from e

        print(completion)
        return LLMResponse(
            content=choice.message.content,
            reasoning_content=choice.message.reasoning_content,
            finish_reason=choice.finish_reason,
            tool_call_requests=tool_call_requests,
            usage=usage,
        )

    async def stream_chat(self, messages: list[Message], tools: list[dict] | None = None):
        """流式发送消息列表，逐步 yield 增量片段，结束时 yield 完整 LLMResponse

        Args:
            messages: 完整的对话消息列表，按时间顺序排列
            tools: 工具 schema 列表，None 或空表示本次不提供工具

        Yields:
            StreamChunk: 正文或推理过程的增量片段
            LLMResponse: 流结束时的最终完整结果
        """
        try:
            kwargs: dict = {
                "model": self._model,
                "messages": [m.to_dict() for m in messages],
                "stream": True,
                # 让供应商在流结束时附带 token 用量（usage 只会出现在最后一个 chunk）
                "stream_options": {"include_usage": True},
            }
            if self._params is not None:
                for name in ("temperature", "top_p", "max_tokens"):
                    value = getattr(self._params, name)
                    if value is not None:
                        kwargs[name] = value
                if self._params.top_k is not None:
                    kwargs["extra_body"] = {"top_k": self._params.top_k}
            if tools is not None:
                kwargs["tools"] = tools

            stream = await self._client.chat.completions.create(**kwargs)

            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            # index -> {"id": str, "name": str, "arguments": str(分片拼接)}
            tool_calls_acc: dict[int, dict] = {}
            finish_reason: str | None = None
            usage = Usage()

            async for chunk in stream:
                if chunk.usage is not None:
                    usage = Usage(
                        prompt_tokens=chunk.usage.prompt_tokens,
                        completion_tokens=chunk.usage.completion_tokens,
                        total_tokens=chunk.usage.total_tokens,
                    )
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                # reasoning_content 是部分供应商（方舟/DeepSeek 等）的扩展字段，SDK 类型上没有
                reasoning_delta = getattr(delta, "reasoning_content", None)
                if reasoning_delta:
                    reasoning_parts.append(reasoning_delta)
                    yield StreamChunk(reasoning_content=reasoning_delta)
                if delta.content:
                    content_parts.append(delta.content)
                    yield StreamChunk(content=delta.content)
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        acc = tool_calls_acc.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            acc["id"] = tc.id
                        if tc.function and tc.function.name:
                            acc["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            acc["arguments"] += tc.function.arguments
                        # 工具调用增量同时对外发布：id/name 仅首个片段携带，后续片段只有参数碎片
                        yield StreamChunk(
                            tool_index=tc.index,
                            tool_id=tc.id,
                            tool_name=tc.function.name if tc.function and tc.function.name else None,
                            tool_arguments_delta=tc.function.arguments if tc.function and tc.function.arguments else None,
                        )
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

            tool_call_requests = None
            if finish_reason == "tool_calls" and tool_calls_acc:
                tool_call_requests = [
                    ToolCallRequest(
                        id=acc["id"],
                        name=acc["name"],
                        arguments=json.loads(acc["arguments"] or "{}"),
                    )
                    for _, acc in sorted(tool_calls_acc.items())
                ]
            elif finish_reason == "length" and tool_calls_acc:
                # max_tokens 截断：截断流里攒出的参数可能不完整，交给检查函数判定
                tool_call_requests = self.check_truncated_tool_calls(
                    finish_reason, [acc for _, acc in sorted(tool_calls_acc.items())]
                )
        except openai.APIError as e:
            logger.debug(f"llm stream 错误 {e}")
            raise _classify_openai_error(e) from e

        yield LLMResponse(
            content="".join(content_parts) or None,
            reasoning_content="".join(reasoning_parts) or None,
            finish_reason=finish_reason,
            tool_call_requests=tool_call_requests,
            usage=usage,
        )

if __name__ == "__main__":
    import asyncio
    import os
    from dotenv import load_dotenv
    from myagent.agent.core.tool import Tool
    from typing import Any
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

        def execute(self, **kwargs) -> str:
            date = kwargs.get("date", None)
            return f"{date}的天气是晴天"
    load_dotenv()
    print(os.getenv("ARK_API_KEY"))
    llm= OpenAIProvider("doubao-seed-1-6-flash-250828",api_key=os.getenv("ARK_API_KEY"),base_url="https://ark.cn-beijing.volces.com/api/v3")
    
    message = Message("user","请查询2026-07-12号的天气")
    llm_response:LLMResponse = asyncio.run(llm.chat([message],[WeatherTool().to_schema()]))
    print(llm_response.reasoning_content)
    print(llm_response.content)
    print(llm_response.tool_call_requests)
    print(WeatherTool().execute(**llm_response.tool_call_requests[0].arguments))

    # 流式输出自测
    async def run_stream():
        printed_reasoning = False  # 推理标签只打一次，后续片段直接续打
        async for item in llm.stream_chat([message], [WeatherTool().to_schema()]):
            if isinstance(item, LLMResponse):
                print("\n--- 流式最终结果 ---")
                print("reasoning:", item.reasoning_content)
                print("content:", item.content)
                print("tool_calls:", item.tool_call_requests)
                print("usage:", item.usage)
            elif item.reasoning_content:
                if not printed_reasoning:
                    printed_reasoning = True
                print(item.reasoning_content, end="", flush=True)
            elif item.tool_id:
                # 工具调用首片段：携带 id 与工具名
                print(f"\n[tool_call {item.tool_index}] {item.tool_name}: ", end="", flush=True)
            elif item.tool_arguments_delta is not None:
                print(item.tool_arguments_delta, end="", flush=True)
            elif item.content:
                print(item.content, end="", flush=True)
    asyncio.run(run_stream())