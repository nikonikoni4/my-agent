"""OpenAI 兼容格式的 LLM 实现。

任何提供 chat.completions 兼容接口的供应商都可用，
火山方舟只需 base_url 传入 https://ark.cn-beijing.volces.com/api/v3。
"""
from openai import AsyncOpenAI
import openai
import json
from myagent.agent.core.provider import ChatParams, LLMProvider, LLMResponse, Message, StreamChunk, ToolCallRequest, Usage
from myagent.agent.execption import LLMCallError
import logging 
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
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
            tool_call_requests=self.parse_tool_call(choice.message) if choice.finish_reason=='tool_calls' else None
        except openai.APIError as e :
            logger.debug(f"llm call 错误 {e}")
            raise LLMCallError(f"llm call 错误：{e}") from e

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
        except openai.APIError as e:
            logger.debug(f"llm stream 错误 {e}")
            raise LLMCallError(f"llm stream 错误：{e}") from e

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