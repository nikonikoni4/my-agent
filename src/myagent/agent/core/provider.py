"""LLM 调用的契约层：定义消息、采样参数和 Provider 接口。

本模块属于 core/，只依赖标准库和 typing。
具体实现（如 OpenAI 兼容接口）由 llm/ 层提供，通过构造函数注入。
"""
from abc import ABC, abstractmethod
import json
from dataclasses import dataclass,field
from typing import Any
import datetime
from myagent.agent.execption import LLMToolCallTruncatedError
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
    def to_dict(self)->dict: # 需要把这个改为to_llm_call_dict
        """
        将message转化为 OpenAI wire 格式的 dict

        Raises:
            ValueError: role 非法或为空；非 assistant 消息 content 为空；
                assistant 消息 content 与 tool_calls 同时为空；tool 消息缺少 tool_call_id
        """
        if not self.role:
            raise ValueError("role 为空")
        if self.role not in ["assistant", "user",  "tool"]: # system 提示词不写入，而是动态编排
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
@dataclass
class Usage:
    """一次调用的 token 用量统计

    Attributes:
        prompt_tokens: 输入消耗的 token 数
        completion_tokens: 输出消耗的 token 数
        total_tokens: 总 token 数
    """
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

@dataclass
class LLMResponse:
    """一次对话调用的结果

    Attributes:
        content: 模型回复的文本内容
        reasoning_content : 推理过程
        tool_call_requests: 模型发起的工具调用请求列表，无调用时为空列表
        finish_reason: 结束原因，如 stop（正常结束）、length（达到 max_tokens）
        usage: token 用量统计，供应商未返回时各字段为 0
    """
    content: str | None
    reasoning_content : str  | None = None
    tool_call_requests : list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str | None = None
    usage: Usage  = field(default_factory=Usage)
    interrupted : bool = False 

@dataclass
class StreamChunk:
    """流式输出过程中的一个增量片段

    Attributes:
        content: 正文增量文本，本次片段没有则为 None
        reasoning_content: 推理过程增量文本，本次片段没有则为 None
        tool_index: 工具调用增量的槽位号（并行调用时的第几个调用），非工具片段为 None
        tool_id: 工具调用标识，仅每个调用的首个片段携带，后续片段为 None
        tool_name: 工具名，仅每个调用的首个片段携带，后续片段为 None
        tool_arguments_delta: 参数 JSON 的增量碎片。单个碎片不是合法 JSON，
            只能在流结束后拼接再解析
    """
    content: str | None = None
    reasoning_content: str | None = None
    tool_index: int | None = None
    tool_id: str | None = None
    tool_name: str | None = None
    tool_arguments_delta: str | None = None
        

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
    def __init__(self,model: str,params: ChatParams | None = None):
        self._params = params if params else None
        self._model = model

    @property
    def model(self) -> str:
        """本次使用的模型名，供调用方写 request/header 快照"""
        return self._model

    @property
    def params(self) -> ChatParams | None:
        """采样参数，None 表示全部使用供应商默认值"""
        return self._params

    @abstractmethod
    async def chat(self, messages: list[Message],tools : list[dict] | None = None ) -> LLMResponse:
        """发送消息列表，返回模型回复

        Args:
            messages: 完整的对话消息列表，按时间顺序排列
            tools: 工具 schema 列表（各工具 to_schema() 的输出），None 或空表示本次不提供工具

        Returns:
            LLMResponse: 模型回复及结束原因、token 用量
        """
    @abstractmethod
    def stream_chat(self, messages: list[Message], tools: list[dict] | None = None):
        """流式发送消息列表，逐步返回增量片段

        Args:
            messages: 完整的对话消息列表，按时间顺序排列
            tools: 工具 schema 列表（各工具 to_schema() 的输出），None 或空表示本次不提供工具

        Yields:
            StreamChunk: 正文或推理过程的增量片段
            LLMResponse: 流结束时的最终完整结果，含工具调用、finish_reason、token 用量
        """

    def parse_tool_call(self,response_message)->list[ToolCallRequest]:
        """把 SDK 响应里的 tool_calls 解析为项目内的 ToolCallRequest 列表。

        Args:
            response_message: SDK 响应中的 choices[0].message 对象，
                可为 None（部分供应商无工具调用时该位置为空）。

        Returns:
            ToolCallRequest 列表，arguments 已从 JSON 字符串解析为 dict；
            无工具调用时返回 None（输入为 None）或空列表。
        """
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

    def check_truncated_tool_calls(self, finish_reason: str | None, raw_tool_calls: list[dict] | None) -> list[ToolCallRequest] | None:
        """max_tokens 截断（finish_reason == 'length'）时的工具调用完整性检查。

        截断响应可能携带未生成完的工具调用：id/name 完整，但 arguments 的
        JSON 字符串在中途被切断。逐个尝试 json.loads：
        - 全部可解析：返回 ToolCallRequest 列表，交由上层继续处理
        - 任一不可解析：抛 LLMToolCallTruncatedError，由 agent loop 决定恢复策略

        Args:
            finish_reason: 本次响应的结束原因，非 'length' 时不做检查
            raw_tool_calls: 响应携带的原始工具调用，每项形如
                {"id": str, "name": str, "arguments": str}，
                arguments 为未解析的 JSON 字符串

        Returns:
            finish_reason 非 'length'、或无工具调用时返回 None；
            否则返回解析成功的 ToolCallRequest 列表。

        Raises:
            LLMToolCallTruncatedError: 存在参数 JSON 不完整（被截断）的工具调用时抛出，
                details["truncated_tool_calls"] 携带全部原始工具调用信息。
        """
        if finish_reason != "length" or not raw_tool_calls:
            return None
        requests: list[ToolCallRequest] = []
        broken: list[str] = []
        for tc in raw_tool_calls:
            try:
                arguments = json.loads(tc.get("arguments") or "{}")
            except json.JSONDecodeError as e:
                broken.append(
                    f"id={tc.get('id')!r} name={tc.get('name')!r} "
                    f"截断于第 {e.pos} 字符（{e.msg}）"
                )
                continue
            requests.append(
                ToolCallRequest(id=tc.get("id", ""), name=tc.get("name", ""), arguments=arguments)
            )
        if broken:
            raise LLMToolCallTruncatedError(
                f"输出达到 max_tokens 被截断，{len(broken)}/{len(raw_tool_calls)} 个工具调用参数不完整，"
                f"无法解析：{'；'.join(broken)}",
                code="LLM_TOOL_CALL_TRUNCATED",
                details={"truncated_tool_calls": raw_tool_calls},
            )
        return requests