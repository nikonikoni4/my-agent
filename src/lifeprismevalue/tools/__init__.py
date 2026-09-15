"""lifeprismevalue.tools 工具包

提供从 lifeprism 移植的数据工具与文件系统工具，以及注册表构建函数。
"""

from __future__ import annotations

from typing import Any

from myagent.agent.core.tool.tool import Tool

from lifeprismevalue.tools.custom_records_tool import (
    CreateCustomRecordEntryTool,
    CreateCustomRecordTypeTool,
    ListCustomRecordTypesTool,
    QueryCustomRecordEntriesTool,
)
from lifeprismevalue.tools.filesystem import (
    EditFileTool,
    FileTreeTool,
    ReadFileTool,
    SearchFileTool,
    SearchStringTool,
    WriteFileTool,
)
from lifeprismevalue.tools.habit_tool import (
    BackfillCheckinTool,
    CancelCheckinHabitTool,
    CheckinHabitTool,
    QueryUserHabitsTool,
)
from lifeprismevalue.tools.lifeprismsystem import (
    UpdateUserBehaviorNoteTool,
    UserActivitySummaryTool,
    UserComputerLogTool,
    UserMoodCreateTool,
    UserMoodQuryTool,
)

_LIFEPRISM_TOOL_CLASSES: list[type[Tool]] = [
    # 行为数据
    UserActivitySummaryTool,
    UserComputerLogTool,
    UpdateUserBehaviorNoteTool,
    # 心情
    UserMoodQuryTool,
    UserMoodCreateTool,
    # 习惯
    QueryUserHabitsTool,
    CheckinHabitTool,
    CancelCheckinHabitTool,
    BackfillCheckinTool,
    # 自定义记录
    ListCustomRecordTypesTool,
    CreateCustomRecordTypeTool,
    CreateCustomRecordEntryTool,
    QueryCustomRecordEntriesTool,
    # 文件系统
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
    FileTreeTool,
    SearchFileTool,
    SearchStringTool,
]


def build_lifeprism_tools() -> list[Tool]:
    """构建全部移植工具的实例列表，可直接注册进 myagent 的工具注册表。

    Example:
        register.register(build_lifeprism_tools())
    """
    return [cls() for cls in _LIFEPRISM_TOOL_CLASSES]


__all__ = [
    "build_lifeprism_tools",
    "UserActivitySummaryTool",
    "UserComputerLogTool",
    "UpdateUserBehaviorNoteTool",
    "UserMoodQuryTool",
    "UserMoodCreateTool",
    "QueryUserHabitsTool",
    "CheckinHabitTool",
    "CancelCheckinHabitTool",
    "BackfillCheckinTool",
    "ListCustomRecordTypesTool",
    "CreateCustomRecordTypeTool",
    "CreateCustomRecordEntryTool",
    "QueryCustomRecordEntriesTool",
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "FileTreeTool",
    "SearchFileTool",
    "SearchStringTool",
]
