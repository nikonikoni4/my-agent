from typing import Literal
from myagent.infra.events.payload import (
    Payload,
    TurnStartPayload,
    StepStartPayload,
    RequestHeaderPayload,
    UserMessagePayload,
    AssistantChunkPayload,
    AssistantMessagePayload,
    ToolCallPayload,
    ToolResultPayload,
    StepEndPayload,
    TurnEndPayload,
    SessionEventPayload
)

class EventSpec:
    """
    事件类型约定
    """
    def __init__(self,name:str,semantics:Literal["waterfall","emit"],payload_type:type[Payload]):
        self.name = name
        self.semantics = semantics
        self.payload_type = payload_type

# evnet事件集中定义，内容过多时分模块文件导出
# 事件名称遵循 域/动作 格式
# 注意：本文件只登记运行时事件。chunk 的打包存储行（content-chunks 等）是
# session 落盘时的存储编码，不是运行时事件，登记在 core/session/types.py
# 触发语义目前统一为 emit，waterfall 类型后续按需添加

TURN_START = EventSpec("turn/start", "emit", TurnStartPayload)
STEP_START = EventSpec("step/start", "emit", StepStartPayload)
REQUEST_HEADER = EventSpec("request/header", "emit", RequestHeaderPayload)
USER_MESSAGE = EventSpec("user/message", "emit", UserMessagePayload)
ASSISTANT_CHUNK = EventSpec("assistant/chunk", "emit", AssistantChunkPayload)
ASSISTANT_MESSAGE = EventSpec("assistant/message", "emit", AssistantMessagePayload)
TOOL_CALL = EventSpec("tool/call", "emit", ToolCallPayload)
TOOL_RESULT = EventSpec("tool/result", "emit", ToolResultPayload)
STEP_END = EventSpec("step/end", "emit", StepEndPayload)
TURN_END = EventSpec("turn/end", "emit", TurnEndPayload)
SESSION_EVENT = EventSpec("session/event", "emit", SessionEventPayload)
# 集中聚合所有事件，用于完整性校验（名称唯一、命名规范）
ALL_EVENTS: list[EventSpec] = [
    TURN_START,
    STEP_START,
    REQUEST_HEADER,
    USER_MESSAGE,
    ASSISTANT_CHUNK,
    ASSISTANT_MESSAGE,
    TOOL_CALL,
    TOOL_RESULT,
    STEP_END,
    TURN_END,
    SESSION_EVENT
]
