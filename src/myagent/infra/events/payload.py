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
class ToolCallPayload(Payload):
    """tool/call : 工具execute时, tool_name, tool_params"""
    pass


@dataclass
class ToolResultPayload(Payload):
    """tool/result : tool_name"""
    pass


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