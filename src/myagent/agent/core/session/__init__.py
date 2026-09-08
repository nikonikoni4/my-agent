"""session 包的对外导出。

核心组件：
- Session：会话主体，负责追加记录、派发事件、派生 LLM 输入消息
- SessionStore：会话管理（create/load/fork），组装 Session 与持久化组件
- SessionPresist：持久化（写），订阅 session/event 异步落盘
- SurfaceManager：模型可见消息序列的视图管理

类型：SessionRecordData 信封 + 各记录类型的 Data 数据类（types.py）。
"""

from myagent.agent.core.session.session import Session
from myagent.agent.core.session.store import SessionStore
from myagent.agent.core.session.persistence import SessionPresist
from myagent.agent.core.session.surface import SurfaceManager
from myagent.agent.core.session.types import (
    SessionData,
    SessionMetaData,
    SessionRecordData,
    TextChunkData,
    TurnStartData,
    StepStartData,
    RequestHeaderData,
    UserMessageData,
    AssistantChunkData,
    AssistantMessageData,
    ToolCallData,
    ToolResultData,
    StepEndData,
    TurnEndData,
    CompactionStartData,
    CompactionSummaryData,
    CompactionEndData,
    ContentChunksData,
    ReasoningChunksData,
    ToolCallChunksData,
)

__all__ = [
    "Session",
    "SessionStore",
    "SessionPresist",
    "SurfaceManager",
    "SessionData",
    "SessionMetaData",
    "SessionRecordData",
    "TextChunkData",
    "TurnStartData",
    "StepStartData",
    "RequestHeaderData",
    "UserMessageData",
    "AssistantChunkData",
    "AssistantMessageData",
    "ToolCallData",
    "ToolResultData",
    "StepEndData",
    "TurnEndData",
    "CompactionStartData",
    "CompactionSummaryData",
    "CompactionEndData",
    "ContentChunksData",
    "ReasoningChunksData",
    "ToolCallChunksData",
]
