"""LLM 调用的契约层：定义消息、采样参数和 Provider 接口。

本模块属于 core/，只依赖标准库和 typing。
具体实现（如 OpenAI 兼容接口）由 llm/ 层提供，通过构造函数注入。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass,field
from typing import Any
import datetime
import json

@dataclass
class RawToolCall:
    """模型发起的一次工具调用（原始形态）。

    arguments 有两种形态，**凭类型区分，不另设标志位**：

    - `dict`：provider 已解析成功。解析只在结果是 JSON 对象时替换，故
      非 str **必然是 dict**；下游（护栏、工具层）直接使用，不再解析。
    - `str`：wire 原样 JSON 字符串。非法 JSON、或解析结果不是对象
      （数组/数字/null 等）时保持原样，由工具层 ToolRegister.parse_call
      解析并以带 hint 的 ToolResult 回喂模型自纠——这类失败不作为 LLM
      调用错误（分类轴归位，见 ADR 工具调用解析与截断处置移入工具层）。

    注意：替换为 dict 后模型当时写的文本不再保留，`to_dict` / `raw_arguments`
    序列化回去的结果是语义等价但字面不同的 JSON（见 raw_arguments）。

    truncated 由 provider 依据响应 finish_reason == "length" 标记：该调用的
    arguments 可能在生成中途被 max_tokens 切断。它是执行语境而非 wire 数据
    （to_dict 不序列化它），供工具层选择回喂话术（截断 vs 语法错误）。
    """
    id : str
    name :str
    arguments : str | dict[str,Any]  # 解析成功为 dict；否则 wire 原样字符串（无参数时为 "{}"）
    truncated : bool = False

    @property
    def raw_arguments(self)->str:
        """wire 形态的 arguments 字符串，供 session 记录与事件 payload 使用。

        str 原样返回（解析失败时即模型当时写的文本）；dict 序列化回去。
        `ensure_ascii=False` 是必须的：默认的 True 会把中文转义成 \\uXXXX，
        参数里带中文时代码体积与 token 都会翻几倍。
        """
        if isinstance(self.arguments,str):
            return self.arguments or "{}"
        return json.dumps(self.arguments,ensure_ascii=False)

    def to_dict(self)->dict:
        if not self.id or not self.name :
            raise ValueError(f"id:{self.id},name:{self.name}中某一个为空")
        return {
            "id":self.id,
            "type" :"function",
            "function":{
                "name":self.name,
                # 契约：wire 格式中 arguments 是 JSON 字符串，故统一走 raw_arguments
                # 取字符串形态。本方法在每次请求都会执行（openai_provider 把历史
                # assistant 消息连同 tool_calls 回放给供应商）
                "arguments" : self.raw_arguments
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
    tool_calls : list[RawToolCall] | None = None
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
        # 允许 system：system 提示词不落 session 的 message list（session 落盘走
        # asdict），而是请求时动态编排到 wire 最前面，故此处必须放行
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
        tool_call_requests: 模型发起的工具调用请求列表（原始形态，arguments
            为 wire 上的 JSON 字符串，解析职责在工具层），无调用时为空列表
        finish_reason: 结束原因，如 stop（正常结束）、length（达到 max_tokens）
        usage: token 用量统计，供应商未返回时各字段为 0
    """
    content: str | None
    reasoning_content : str  | None = None
    tool_call_requests : list[RawToolCall] = field(default_factory=list)
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
            只能在流结束后拼接，拼接结果作为 RawToolCall.arguments 原样
            交给工具层解析
        finish_reason: 结束原因（stop/length/tool_calls 等）。非 None 表示这是
            流正常结束时产出的 finish 块；流中途出错则该块不会被产出，以此
            区分正常结束与中断
    """
    content: str | None = None
    reasoning_content: str | None = None
    tool_index: int | None = None
    tool_id: str | None = None
    tool_name: str | None = None
    tool_arguments_delta: str | None = None
    finish_reason: str | None = None
        

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

    def extract_tool_calls(self,response_message,finish_reason : str | None = None)->list[RawToolCall] | None:
        """把 SDK 响应里的 tool_calls 提取为 RawToolCall 列表，并尽力解析 arguments。

        解析是**尽力而为、静默失败**：只有 json.loads 的结果是 JSON 对象时才把
        arguments 换成 dict；非法 JSON、以及解析成数组/数字/null 等非对象的情况
        一律保持原字符串。不抛异常、不设标志位——类型本身就是标志。

        schema 校验、解析失败的话术与回喂仍由工具层 ToolRegister 承接（信任
        边界没变，只是把"能解析的顺手解析掉"，让护栏等上游拿得到结构化参数）。

        Args:
            response_message: SDK 响应中的 choices[0].message 对象，
                可为 None（部分供应商无工具调用时该位置为空）。
            finish_reason: 本次响应的结束原因，"length" 表示输出被截断。

        Returns:
            RawToolCall 列表；无工具调用时返回 None（输入为 None）或空列表。
        """
        if not response_message:
            return None
        calls = []
        for tool_call in response_message.tool_calls:
            raw = tool_call.function.arguments or "{}"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            # 只有解析结果是 JSON 对象才替换；否则保持原字符串交给工具层判定
            # （非对象如 [1,2] / 123 / null 由工具层回 PARSE_NOT_OBJECT 错误）
            calls.append(RawToolCall(
                id=tool_call.id,
                name=tool_call.function.name,
                arguments=parsed if isinstance(parsed,dict) else raw,
            ))
        if finish_reason == "length":
            for call in calls:
                call.truncated = True
        return calls