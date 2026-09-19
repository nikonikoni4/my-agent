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
    UserActivitySummaryTool,        # 查询行为活动摘要（高使用时段/电脑总览/行为备注/待办等）
    UserComputerLogTool,            # 查询电脑使用详细日志（应用、标题、时长）
    UpdateUserBehaviorNoteTool,     # 创建或更新用户行为备注
    # 心情
    UserMoodQuryTool,               # 查询指定时间段的心情记录
    UserMoodCreateTool,             # 创建心情记录
    # 习惯
    QueryUserHabitsTool,            # 查询用户习惯列表（含挑战进度与今日打卡状态）
    CheckinHabitTool,               # 习惯今日打卡
    CancelCheckinHabitTool,         # 取消习惯今日打卡
    BackfillCheckinTool,            # 习惯补签（补打之前漏掉的日期）
    # 自定义记录
    ListCustomRecordTypesTool,      # 列出自定义记录类型
    CreateCustomRecordTypeTool,     # 创建自定义记录类型
    CreateCustomRecordEntryTool,    # 录入一条自定义记录
    QueryCustomRecordEntriesTool,   # 查询自定义记录条目
    # 文件系统
    ReadFileTool,                   # 读取文件内容
    WriteFileTool,                  # 写入文件
    EditFileTool,                   # 通过内容替换编辑文件
    FileTreeTool,                   # 查看目录树
    SearchFileTool,                 # 按文件名搜索文件
    SearchStringTool,               # 在文件内容中搜索字符串
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
