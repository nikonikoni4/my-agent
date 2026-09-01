"""LLM 调用的契约层：定义消息、采样参数和 Provider 接口。

本模块属于 core/，只依赖标准库和 typing。
具体实现（如 OpenAI 兼容接口）由 llm/ 层提供，通过构造函数注入。
"""
from abc import ABC, abstractmethod
import json
from dataclasses import dataclass,field
from typing import Any
import datetime
@dataclass
class ToolCallRequest:
    id : str
    name :str 
    arguments : dict[str,Any] | None

    def to_dict(self)->dict:
        if not self.id or not self.name :
            raise ValueError(f"id:{self.id},name:{self.name}中某一个为空")
        return {
            "id":self.id,
            "type" :"function",
            "function":{
                "name":self.name,
                # 契约：wire 格式中 arguments 是 JSON 字符串；无参数时为空对象字符串 "{}"
                "arguments" : json.dumps(self.arguments or {})
            }
        }

@dataclass
class Message:
    """一条对话消息

    Attributes:
        role: 角色，取值 system / user / assistant / tool
        content: 消息文本内容
        timestamp : 用于保存消息时添加，llm请求时不会把这个添加到对话中
    """
    role: str
    content: str | list 
    tool_calls : list[ToolCallRequest] | None = None
    tool_call_id : str |None = None 
    reasoning_content : str | None = None 
    timestamp: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    def to_dict(self)->dict:
        """
        将message转化为 OpenAI wire 格式的 dict

        Raises:
            ValueError: role 非法或为空；非 assistant 消息 content 为空；
                assistant 消息 content 与 tool_calls 同时为空；tool 消息缺少 tool_call_id
        """
        if not self.role:
            raise ValueError("role 为空")
        if self.role not in ["assistant", "user", "system", "tool"]:
            raise ValueError(f"{self.role} 不在['assistant','user','system','tool']之中")

        # content 为 None 仅在 assistant 发起工具调用时合法（wire 格式中该消息 content 为 null）
        if self.content is None or self.content == "":
            if not (self.role == "assistant" and self.tool_calls):
                raise ValueError(f"{self.role} 消息 content 为空")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool 消息缺少 tool_call_id，无法与调用配对")

        d = {"role": self.role, "content": self.content}
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = [tool_call.to_dict() for tool_call in self.tool_calls]
        if self.reasoning_content:
            d["reasoning_content"] = self.reasoning_content
        return d
    def to_dict_with_timestamp(self)->dict:
        d= self.to_dict()
        d['timestamp'] = self.timestamp
        return d
@dataclass
class LLMResponse:
    """一次对话调用的结果

    Attributes:
        content: 模型回复的文本内容
        reasoning_content : 推理过程
        finish_reason: 结束原因，如 stop（正常结束）、length（达到 max_tokens）
        usage: token 用量统计，格式为
            {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}，
            供应商未返回时为 None
    """
    content: str | None
    reasoning_content : str  | None = None
    tool_call_requests : list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict  = field(default_factory=dict)


@dataclass
class ChatParams:
    """采样参数，字段为 None 时使用供应商默认值

    Attributes:
        temperature: 随机性，0~2，越小越确定
        top_p: 核采样阈值，0~1
        top_k: 只在概率前 k 的词中采样。OpenAI 标准外参数，
            由实现层通过 extra_body 传递，供应商不支持时会报错或被忽略
        max_tokens: 单次回复的最大 token 数
    """
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    max_tokens: int | None = None





class LLMProvider(ABC):
    """LLM 调用接口，由上层注入具体实现（依赖倒置）"""

    @abstractmethod
    async def chat(self, messages: list[Message], params: ChatParams | None = None) -> LLMResponse:
        """发送消息列表，返回模型回复

        Args:
            messages: 完整的对话消息列表，按时间顺序排列
            params: 采样参数，None 表示全部使用供应商默认值

        Returns:
            LLMResponse: 模型回复及结束原因、token 用量
        """
    def parse_tool_call(self,response_message)->list[ToolCallRequest]:
        if not response_message:
            return 
        tool_call_list = []
        for tool_call in response_message.tool_calls :
            tool_call_list.append(
                ToolCallRequest(
                    id = tool_call.id,
                    name = tool_call.function.name,
                    arguments=json.loads(tool_call.function.arguments)
                )
            )
        return tool_call_list