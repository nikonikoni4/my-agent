"""OpenAI 兼容格式的 LLM 实现。

任何提供 chat.completions 兼容接口的供应商都可用，
火山方舟只需 base_url 传入 https://ark.cn-beijing.volces.com/api/v3。
"""
from openai import AsyncOpenAI
import openai
from myagent.agent.core.provider import ChatParams, LLMProvider, LLMResponse, Message
from myagent.agent.execption import LLMCallError
import logging 
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
class OpenAIProvider(LLMProvider):
    """基于 openai SDK 的实现，model 和 base_url 由调用方指定"""

    def __init__(self, model: str, api_key: str, base_url: str | None = None,chat_params:ChatParams|None = None):
        """
        Args:
            model: 模型名或接入点 ID（如火山方舟的 ep-xxx）
            api_key: 供应商的 API Key
            base_url: 兼容接口地址，None 表示使用 OpenAI 官方地址
        """
        super.__init__(chat_params)
        self._model = model
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
            usage = None
            if completion.usage is not None:
                usage = {
                    "prompt_tokens": completion.usage.prompt_tokens,
                    "completion_tokens": completion.usage.completion_tokens,
                    "total_tokens": completion.usage.total_tokens,
                }

            # 工具调用解析
            tool_call_request=self.parse_tool_call(choice.message) if choice.finish_reason=='tool_calls' else None
        except openai.APIError as e :
            logger.debug(f"llm call 错误 {e}")
            raise LLMCallError(f"llm call 错误：{e}") from e

        print(completion)
        return LLMResponse(
            content=choice.message.content,
            reasoning_content=choice.message.reasoning_content,
            finish_reason=choice.finish_reason,
            tool_call_request=tool_call_request,
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
    print(llm_response.tool_call_request)
    print(WeatherTool().execute(**llm_response.tool_call_request[0].arguments))