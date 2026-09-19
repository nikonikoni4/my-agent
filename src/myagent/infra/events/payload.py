from dataclasses import dataclass


@dataclass
class Payload:
    pass


# 事件payload集中定义，与架构设计 agent模块类图设计.md 中 session 事件对应
# 具体字段暂不定义，先占位


@dataclass
class TurnStartPayload(Payload):
    """turn/start : turn 从1开始"""
    pass


@dataclass
class StepStartPayload(Payload):
    """step/start : turn, step 从0开始，一次llm调用和工具调用循环算一次step，循环开始时计数"""
    pass


@dataclass
class RequestHeaderPayload(Payload):
    """request/header : 模型请求前配置写入，reason(init,resume,change), model_name, system_prompt, tools, params"""
    pass


@dataclass
class UserMessagePayload(Payload):
    """user/message : message(role, content)"""
    pass


@dataclass
class AssistantChunkPayload(Payload):
    """assistant/chunk : 流式增量片段，逐片段触发（运行时事件）

    与存储层的打包行（content-chunks 等）区分：本事件一次对应一个 StreamChunk，
    字段与 provider 的 StreamChunk 对应：content / reasoning_content /
    工具调用碎片(tool_index, tool_id, tool_name, tool_arguments_delta)
    """
    pass


@dataclass
class AssistantMessagePayload(Payload):
    """assistant/message : message(role, content, tool_calls)"""
    pass


@dataclass
class ToolCallInfo:
    """payload 侧的一次工具调用。

    字段与 provider 的 RawToolCall 同形，但类型定义留在 infra 内——payload 只依赖
    原语，不反向依赖 agent.core，由 loop 在广播时逐条转换填入。

    arguments 两形态凭类型区分，判据与 RawToolCall 一致：
        dict : provider 已解析成功，下游（护栏、工具层）直接使用，不再解析
        str  : wire 原样 JSON 字符串（非法 JSON、或解析结果不是对象），
               由工具层以带 hint 的 ToolResult 回喂模型自纠
    truncated 由 provider 依据 finish_reason == "length" 标记，供工具层选择回喂话术。
    """
    id: str
    name: str
    arguments: str | dict
    truncated: bool = False


@dataclass
class ToolCallPayload(Payload):
    """tool/call : 一次广播携带本批全部工具调用，供护栏等订阅方在工具执行前查看。

    粒度是**批**不是条：需要整批视野的订阅方（如"这批里有几个写操作"）与逐条
    判断的订阅方（如路径护栏）都从这一个 payload 取数，不必为两种消费方式发
    两种事件；逐条还是整批判断，是订阅方自己的事。
    """
    tool_call_requests: list[ToolCallInfo]



@dataclass
class ToolResultPayload(Payload):
    """tool/result : 一次工具执行的结果，供评估/观测订阅消费。

    字段与 session 的 tool/result 记录同源，由 loop 在写回结果时填充：
    tool_name 工具名；arguments 模型发起的原始参数（wire 上的 JSON 字符串，
    解析失败时即坏 JSON 原文）；is_error 是否失败；error_type 失败分类
    （ToolErrorType 的值，成功为 None）；content 回喂给模型的内容；
    duration_ms 工具执行耗时（墙钟毫秒，由 ToolRegister 测量），无数据为 None。
    """
    tool_name: str = ""
    arguments: str = "{}"
    is_error: bool = False
    error_type: str | None = None
    content: str = ""
    duration_ms: int | None = None


@dataclass
class StepEndPayload(Payload):
    """step/end"""
    pass


@dataclass
class TurnEndPayload(Payload):
    """turn/end : reason(success, interrupted)"""
    pass

@dataclass 
class SessionEventPayload[T](Payload):
    "session/event"
    session_record : T 

@dataclass
class RequestErrorPayLoad[T](Payload):
    error_type : T # 暂定