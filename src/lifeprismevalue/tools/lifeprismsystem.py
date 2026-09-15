"""行为 / 心情数据工具

移植自 lifeprism/llm/agent/tools/lifeprismsystem.py。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from myagent.agent.core.tool.tool import Tool

from lifeprismevalue import data as lw
from lifeprismevalue.data import behavior as behavior_data
from lifeprismevalue.data import mood as mood_data
from lifeprismevalue.tools.base import ERROR, SUCCESS, _result
from lifeprismevalue.utils.density import build_time_segments
from lifeprismevalue.utils.time_utils import local_to_utc_iso, utc_to_local_display

_TIME_FORMAT_DESC = "YYYY-MM-DD HH:MM:SS（本地时区）"


def _parse_iso_time(time_str: str) -> datetime:
    """解析 ISO 8601 时间字符串，返回 UTC aware datetime（内部使用）。"""
    try:
        dt = datetime.fromisoformat(str(time_str))
    except ValueError as e:
        raise ValueError(f"{ERROR} 时间格式错误: {e}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _utc_to_local(utc_time_str: str) -> str:
    """将 UTC ISO 时间字符串转换为本地时区显示格式。"""
    if not utc_time_str:
        return ""
    try:
        return utc_to_local_display(str(utc_time_str))
    except (ValueError, TypeError):
        return str(utc_time_str)


# ==================== 用户活动汇总 ====================


def _category_stats(logs: list[dict], segment_start_time: str, segment_end_time: str) -> dict:
    """计算某段时间内的分类占比（若 log 区间在边界，依据边界截断）。"""
    segment_start = _parse_iso_time(segment_start_time)
    segment_end = _parse_iso_time(segment_end_time)

    category_durations = {}
    total_duration = 0

    for log in logs:
        log_start = _parse_iso_time(log["start_time"])
        log_end = _parse_iso_time(log["end_time"])
        actual_start = max(log_start, segment_start)
        actual_end = min(log_end, segment_end)
        if actual_start >= actual_end:
            continue
        duration = (actual_end - actual_start).total_seconds()
        category = log.get("category_name", "未分类")
        category_durations[category] = category_durations.get(category, 0) + duration
        total_duration += duration

    if total_duration == 0:
        return {}
    return {cat: (dur / total_duration * 100) for cat, dur in category_durations.items()}


def query_user_activity_summary(query_option: set[str], start_time: str, end_time: str) -> str:
    """查询 LifePrism 系统的行为活动数据，返回格式化文本。"""
    allowed_options = {
        "high_usage_segments",
        "computer_overview",
        "user_behavior_notes",
        "ai_behavior_notes",
        "todolist",
    }
    invalid_options = set(query_option) - allowed_options
    if invalid_options:
        raise ValueError(f"{ERROR} Invalid query options: {invalid_options}")

    start_dt = _parse_iso_time(start_time)
    end_dt = _parse_iso_time(end_time)
    if start_dt >= end_dt:
        raise ValueError(f"{ERROR} start_time must be before end_time")

    parts = []

    if "high_usage_segments" in query_option:
        app_log = behavior_data.query_computer_usage_with_names(
            start_time, end_time,
            fields=["start_time", "end_time", "app", "title", "duration", "category_id", "sub_category_id"],
        )
        if not app_log:
            parts.append("## 电脑高频使用时段分析 \n 该时间段没有电脑使用记录")
        else:
            usage_time_segments = build_time_segments(app_log, start_time, end_time, 0.6, 6)
            content = "## 电脑高频使用时段分析\n"
            for idx, segment in enumerate(usage_time_segments, 1):
                category_stats = _category_stats(app_log, segment["start"], segment["end"])
                content += f"### 时间段 {idx}: {_utc_to_local(segment['start'])} ~ {_utc_to_local(segment['end'])}\n"
                content += f"持续时长: {segment['duration_seconds'] // 60} 分钟\n"
                content += "分类占比:\n"
                for category, percentage in sorted(
                    category_stats.items(), key=lambda x: x[1], reverse=True
                ):
                    content += f"  - {category}: {percentage:.1f}%\n"
            parts.append(content)

    if "computer_overview" in query_option:
        app_log = behavior_data.query_computer_usage_with_names(
            start_time, end_time,
            fields=["start_time", "end_time", "app", "title", "duration", "category_id", "sub_category_id"],
        )
        if not app_log:
            parts.append("## 电脑总体使用统计 \n 该时间段没有电脑使用记录")
        else:
            segment_start = _parse_iso_time(start_time)
            segment_end = _parse_iso_time(end_time)
            category_durations = {}
            total_duration = 0
            for log in app_log:
                log_start = _parse_iso_time(log["start_time"])
                log_end = _parse_iso_time(log["end_time"])
                actual_start = max(log_start, segment_start)
                actual_end = min(log_end, segment_end)
                if actual_start >= actual_end:
                    continue
                duration = (actual_end - actual_start).total_seconds()
                category = log.get("category_name", "未分类")
                category_durations[category] = category_durations.get(category, 0) + duration
                total_duration += duration

            content = "## 电脑总体使用统计\n"
            content += f"总使用时长: {total_duration // 60} 分钟\n"
            content += "分类统计:\n"
            for category, duration in sorted(
                category_durations.items(), key=lambda x: x[1], reverse=True
            ):
                percentage = (duration / total_duration * 100) if total_duration > 0 else 0
                content += f"  - {category}: {duration // 60} 分钟 ({percentage:.1f}%)\n"
            parts.append(content)

    if "user_behavior_notes" in query_option:
        custom_blocks = behavior_data.query_custom_blocks(
            start_time, end_time, fields=["id", "start_time", "end_time", "content"]
        )
        if not custom_blocks:
            parts.append("## 用户自定义行为备注 \n 用户自定义行为备注为空")
        else:
            content = "## 用户自定义行为备注\n"
            for i in range(len(custom_blocks)):
                content += (
                    f"{i}. 'block_id': {custom_blocks[i]['id']}, "
                    f"{_utc_to_local(custom_blocks[i]['start_time'])}~"
                    f"{_utc_to_local(custom_blocks[i]['end_time'])} : {custom_blocks[i]['content']}\n"
                )
            parts.append(content)

    if "ai_behavior_notes" in query_option:
        behaviors = behavior_data.query_behaviors(
            start_time, end_time, fields=["start_time", "end_time", "behavior_summary"]
        )
        if not behaviors:
            parts.append("## AI分析行为备注 \n AI分析行为备注为空")
        else:
            content = "## AI分析行为备注\n"
            for i in range(len(behaviors)):
                content += (
                    f"{i}. {_utc_to_local(behaviors[i]['start_time'])}~"
                    f"{_utc_to_local(behaviors[i]['end_time'])} : "
                    f"{behaviors[i]['behavior_summary']}\n"
                )
            parts.append(content)

    if "todolist" in query_option:
        local_start_date = utc_to_local_display(start_time)[:10]
        local_end_date = utc_to_local_display(end_time)[:10]
        todolists = behavior_data.query_todos(
            local_start_date, local_end_date, fields=["content", "date", "state"]
        )

        def _format_state(state: str) -> str:
            state_map = {"scheduled": "未完成", "completed": "已完成"}
            return state_map.get(state, state)

        filtered_todos = [t for t in todolists if t.get("state") in ("scheduled", "completed")]
        if not filtered_todos:
            parts.append("## 用户待办事项 \n 用户待办事项为空")
        else:
            from collections import defaultdict

            by_date = defaultdict(list)
            for todo in filtered_todos:
                by_date[todo["date"]].append((todo["content"], todo["state"]))
            content = "## 用户待办事项\n"
            for date in sorted(by_date.keys()):
                content += f"### {date}\n"
                for idx, (item, state) in enumerate(by_date[date], 1):
                    content += f"{idx}. {item} [{_format_state(state)}]\n"
            parts.append(content)

    return "\n".join(parts)


class UserActivitySummaryTool(Tool):
    """数据查询工具"""

    @property
    def name(self) -> str:
        return "query_user_activity_summary"

    @property
    def description(self) -> str:
        return (
            "查询lifeprism系统中用户的行为活动数据，包括\n"
            "1. high_usage_segments ： 电脑高频使用时段数据分析，按高密度时间段分组展示分类占比。\n"
            "2. computer_overview ： 电脑总体统计数据，展示整个时间段内各分类的总时间和占比。\n"
            "3. user_behavior_notes ： 用户对于某段时间的自定义行为备注，是了解用户行为最直接的数据。\n"
            "4. ai_behavior_notes ： AI对于某段时间的截图分析，由AI经过截图分析，不一定准确，仅供参考。\n"
            "5. todolist ： 用户在这段时间内的任务列表。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query_option": {
                    "type": "array",
                    "description": "查询选项列表",
                    "items": {
                        "type": "string",
                        "enum": [
                            "high_usage_segments",
                            "computer_overview",
                            "user_behavior_notes",
                            "ai_behavior_notes",
                            "todolist",
                        ],
                    },
                    "minItems": 1,
                },
                "start_time": {"type": "string", "description": f"查询开始时间，格式：{_TIME_FORMAT_DESC}"},
                "end_time": {"type": "string", "description": f"查询结束时间，格式：{_TIME_FORMAT_DESC}"},
            },
            "required": ["query_option", "start_time", "end_time"],
        }

    async def execute(self, **kwargs: Any):
        try:
            query_option = set(kwargs.get("query_option", []))
            start_time = local_to_utc_iso(kwargs.get("start_time", ""))
            end_time = local_to_utc_iso(kwargs.get("end_time", ""))
            return _result(query_user_activity_summary(query_option, start_time, end_time))
        except ValueError as e:
            return _result(f"{ERROR}参数错误: {str(e)}")
        except Exception as e:
            return _result(f"{ERROR}查询失败: {str(e)}")


# ==================== 用户电脑使用日志 ====================


def _format_duration(seconds: int) -> str:
    """将秒数格式化为可读时长字符串。"""
    if seconds < 60:
        return f"{seconds}秒"

    minutes = seconds // 60
    remaining_seconds = seconds % 60

    if minutes < 60:
        if remaining_seconds > 0:
            return f"{minutes}分{remaining_seconds}秒"
        return f"{minutes}分"

    hours = minutes // 60
    remaining_minutes = minutes % 60

    if remaining_minutes > 0 and remaining_seconds > 0:
        return f"{hours}小时{remaining_minutes}分{remaining_seconds}秒"
    elif remaining_minutes > 0:
        return f"{hours}小时{remaining_minutes}分"
    elif remaining_seconds > 0:
        return f"{hours}小时{remaining_seconds}秒"
    return f"{hours}小时"


def query_user_activity_log(start_time: str, end_time: str, duration_min: int = 45) -> str:
    """查询用户电脑使用的详细日志。"""
    MAX_LEN = 40
    result = f"[日志查询说明] 查询结果屏蔽了持续时间小于{duration_min}秒的记录。\n\n"

    app_log = behavior_data.query_computer_usage_with_names(
        start_time,
        end_time,
        fields=["start_time", "end_time", "app", "title", "duration", "category_id"],
    )

    app_log = [log for log in app_log if log.get("duration", 0) >= duration_min]

    if not app_log:
        return f"该时间段内没有持续时长大于等于{duration_min}秒的电脑使用记录。"

    if len(app_log) > MAX_LEN:
        total_log = len(app_log)
        app_log = app_log[:MAX_LEN]
        result += (
            f"注意：当前搜索区间过大，共{total_log}条记录，仅展示前{MAX_LEN}条记录。"
            f"展示的时间范围为：{_utc_to_local(app_log[0]['start_time'])} ~ "
            f"{_utc_to_local(app_log[-1]['end_time'])}。\n\n"
        )

    result += "查询结果：\n"
    for log in app_log:
        start = _utc_to_local(log.get("start_time", ""))
        end = _utc_to_local(log.get("end_time", ""))
        app = log.get("app", "未知应用")
        title = log.get("title", "无标题")
        duration_sec = log.get("duration", 0)
        category = log.get("category_name", "未分类")
        duration_str = _format_duration(duration_sec)
        result += f"{start} ~ {end} {app} {title} {duration_str} {category}\n"

    return result.strip()


class UserComputerLogTool(Tool):
    """用户电脑使用日志工具"""

    @property
    def name(self) -> str:
        return "query_user_activity_log"

    @property
    def description(self) -> str:
        return (
            "查询用户电脑使用的详细日志，返回格式化的活动记录。\n"
            "适用场景：需要查看用户在某个时间段内具体使用了哪些应用、窗口标题、使用时长等详细信息。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "start_time": {"type": "string", "description": f"查询开始时间，格式：{_TIME_FORMAT_DESC}"},
                "end_time": {"type": "string", "description": f"查询结束时间，格式：{_TIME_FORMAT_DESC}"},
                "duration_min": {
                    "type": "integer",
                    "description": "最小持续时长（秒），只返回持续时长大于等于此值的记录，默认45秒",
                    "default": 45,
                },
            },
            "required": ["start_time", "end_time"],
        }

    async def execute(self, **kwargs: Any):
        try:
            start_time = local_to_utc_iso(kwargs.get("start_time", ""))
            end_time = local_to_utc_iso(kwargs.get("end_time", ""))
            duration_min = kwargs.get("duration_min", 45)
            if not start_time or not end_time:
                return _result(f"{ERROR}参数错误: start_time 和 end_time 是必填参数")
            if not duration_min:
                duration_min = 45
            return _result(query_user_activity_log(start_time, end_time, duration_min))
        except ValueError as e:
            return _result(f"{ERROR}参数错误: {str(e)}")
        except Exception as e:
            return _result(f"{ERROR}查询失败: {str(e)}")


# ==================== 创建 / 更新用户行为备注 ====================


def create_or_update_user_behavior_note(
    start_time: str, end_time: str, content: str, block_id: int | None = None
) -> str:
    """创建或更新用户行为备注。"""
    start_dt = _parse_iso_time(start_time)
    end_dt = _parse_iso_time(end_time)
    if start_dt >= end_dt:
        raise ValueError(f"{ERROR} 开始时间必须早于结束时间")

    duration = int((end_dt - start_dt).total_seconds() / 60)
    color = "#bfdbfe"
    data = {
        "start_time": start_time,
        "end_time": end_time,
        "content": content,
        "duration": duration,
        "color": color,
    }

    try:
        if block_id is not None:
            result = behavior_data.update_custom_block(block_id, data)
            if result:
                return (
                    f"{SUCCESS}更新行为备注成功 (ID: {block_id})\n"
                    f"时间段: {_utc_to_local(start_time)} ~ {_utc_to_local(end_time)}\n"
                    f"内容: {content}\n时长: {duration} 分钟"
                )
            else:
                return f"{ERROR}更新失败: 未找到 ID 为 {block_id} 的记录"
        else:
            result = behavior_data.create_custom_block(data)
            if result:
                new_id = result.get("id", "未知")
                return (
                    f"{SUCCESS}创建行为备注成功 (ID: {new_id})\n"
                    f"时间段: {_utc_to_local(start_time)} ~ {_utc_to_local(end_time)}\n"
                    f"内容: {content}\n时长: {duration} 分钟"
                )
            else:
                return f"{ERROR}创建失败: 未知错误"
    except Exception as e:
        raise Exception(f"数据库操作失败: {str(e)}") from e


class UpdateUserBehaviorNoteTool(Tool):
    """创建或更新用户行为备注工具"""

    @property
    def name(self) -> str:
        return "create_or_update_user_behavior_note"

    @property
    def description(self) -> str:
        return (
            "创建或更新用户对某段时间的自定义行为备注。\n"
            "适用场景：用户想要记录或修改某个时间段内做了什么事情。\n"
            "如果提供 block_id 则更新现有记录，block_id可通过query_user_activity_summary工具获取。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "start_time": {"type": "string", "description": f"开始时间，格式：{_TIME_FORMAT_DESC}"},
                "end_time": {"type": "string", "description": f"结束时间，格式：{_TIME_FORMAT_DESC}"},
                "content": {"type": "string", "description": "行为备注内容"},
                "block_id": {
                    "type": "integer",
                    "description": "可选，时间块 ID。如果提供则更新现有记录，否则创建新记录。 可以通过query_user_activity_summary工具获取。",
                },
            },
            "required": ["start_time", "end_time", "content"],
        }

    async def execute(self, **kwargs: Any):
        try:
            start_time = local_to_utc_iso(kwargs.get("start_time", ""))
            end_time = local_to_utc_iso(kwargs.get("end_time", ""))
            content = kwargs.get("content", "")
            block_id = kwargs.get("block_id")

            if not start_time or not end_time or not content:
                return _result(f"{ERROR} 参数错误: start_time、end_time 和 content 是必填参数")

            return _result(
                create_or_update_user_behavior_note(
                    start_time=start_time, end_time=end_time, content=content, block_id=block_id
                )
            )
        except ValueError as e:
            return _result(f"{ERROR} 参数错误: {str(e)}")
        except Exception as e:
            return _result(f"{ERROR} 操作失败: {str(e)}")


# ==================== 心情 ====================


def _get_mood_type_ids() -> list[str]:
    return [m["id"] for m in mood_data.get_mood_types()]


def _get_mood_types() -> str:
    return "\n ".join([f"{m['id']}: {m['name']}" for m in mood_data.get_mood_types()])


def query_user_mood(
    start_time: str, end_time: str, by_mood_type_id: str | None = None
) -> str:
    """查询用户在指定时间范围内的心情记录。"""
    mood_entries = mood_data.get_mood_entries(start_time=start_time, end_time=end_time)
    result = []
    if by_mood_type_id:
        for mood_entry in mood_entries:
            if mood_entry["mood_type_id"] == by_mood_type_id:
                result.append(mood_entry)
    else:
        result = mood_entries

    if not result:
        return (
            f"{_utc_to_local(start_time)}~{_utc_to_local(end_time)}  无{by_mood_type_id}对应心情记录"
            if by_mood_type_id
            else f"{_utc_to_local(start_time)}~{_utc_to_local(end_time)}  无心情记录"
        )

    formatted_result = []
    for idx, entry in enumerate(result, 1):
        factors_raw = entry.get("factors", "")
        if factors_raw:
            try:
                factors_list = (
                    json.loads(factors_raw) if isinstance(factors_raw, str) else factors_raw
                )
                factors_str = (
                    ", ".join(factors_list)
                    if isinstance(factors_list, list)
                    else str(factors_list)
                )
            except (json.JSONDecodeError, TypeError):
                factors_str = str(factors_raw)
        else:
            factors_str = ""

        formatted_result.append(
            f"{idx}. {_utc_to_local(entry.get('event_time', ''))} 心情: {entry.get('score', 'N/A')}分\n"
            f"   内容：{entry.get('content', '无') or '无'}\n"
            f"   影响因素: {factors_str if factors_str else '无'}"
        )
    return "\n\n".join(formatted_result)


class UserMoodQuryTool(Tool):
    """查询用户心情记录工具"""

    @property
    def name(self) -> str:
        return "query_user_mood"

    @property
    def description(self) -> str:
        return (
            "查询用户在指定时间范围内的心情记录，包括心情评分、内容和影响因素。\n"
            "返回格式化的心情记录列表，便于用户查看和分析心情变化趋势。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "start_time": {"type": "string", "description": f"查询开始时间，格式：{_TIME_FORMAT_DESC}"},
                "end_time": {"type": "string", "description": f"查询结束时间，格式：{_TIME_FORMAT_DESC}"},
                "by_mood_type_id": {
                    "type": ["string", "null"],
                    "description": f"可选，按心情类型ID过滤，可使用的心情ID类型以及心情名称对(id:name): \n {_get_mood_types()}",
                    "enum": _get_mood_type_ids(),
                },
            },
            "required": ["start_time", "end_time"],
        }

    async def execute(self, **kwargs: Any):
        try:
            start_time = local_to_utc_iso(kwargs.get("start_time", ""))
            end_time = local_to_utc_iso(kwargs.get("end_time", ""))
            by_mood_type_id = kwargs.get("by_mood_type_id")
            return _result(query_user_mood(start_time, end_time, by_mood_type_id))
        except ValueError as e:
            return _result(f"{ERROR}参数错误: {str(e)}")
        except Exception as e:
            return _result(f"{ERROR}查询失败: {str(e)}")


def create_user_mood(
    content: str,
    score: int,
    mood_type_id: str,
    factors_raw: list[str] | None = None,
    event_time: str | None = None,
) -> str:
    """创建用户心情记录。"""
    data = {"content": content, "score": score, "mood_type_id": mood_type_id}
    if factors_raw:
        data["factors"] = json.dumps(factors_raw)
    if event_time:
        data["event_time"] = event_time
    mood_id = mood_data.create_mood_entry(data)
    return f"{SUCCESS}创建心情记录成功，ID: {mood_id}"


class UserMoodCreateTool(Tool):
    """创建用户心情记录工具"""

    @property
    def name(self) -> str:
        return "create_user_mood"

    @property
    def description(self) -> str:
        return (
            "创建用户心情记录，包括心情评分、内容和影响因素。\n"
            "返回创建的记录ID，便于用户查看和管理心情记录。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "心情记录内容，描述当前的心情感受"},
                "mood_type_id": {
                    "type": "string",
                    "description": f"必填，心情类型ID，可使用的心情ID类型以及心情名称对(id:name): \n {_get_mood_types()}",
                    "enum": _get_mood_type_ids(),
                },
                "factors": {
                    "type": "array",
                    "description": "可选，可多选，影响心情的因素列表",
                    "items": {"type": "string", "enum": self._get_factors()},
                },
                "event_time": {
                    "type": "string",
                    "description": "事件发生时间，格式 YYYY-MM-DD HH:MM:SS（本地时间）。不提供则默认当前时间",
                },
            },
            "required": ["content", "mood_type_id"],
        }

    @staticmethod
    def _get_factors() -> list[str]:
        impacts = mood_data.get_mood_impacts()
        return [imp["name"] for imp in impacts]

    async def execute(self, **kwargs: Any):
        try:
            content = kwargs.get("content", "")
            mood_type_id = kwargs.get("mood_type_id", "")
            factors = kwargs.get("factors")
            event_time_raw = kwargs.get("event_time")
            if not mood_type_id:
                return _result(f"{ERROR}请输入心情类型ID")

            mood_type = mood_data.get_mood_type_by_id(mood_type_id)
            if not mood_type:
                return _result(f"{ERROR}心情类型ID {mood_type_id} 不存在")
            score = mood_type.get("score", 50)

            event_time_utc = None
            if event_time_raw:
                if not isinstance(event_time_raw, str):
                    return _result(f"{ERROR}参数错误：event_time 必须是字符串")
                if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", event_time_raw):
                    return _result(f"{ERROR}参数格式错误：event_time 格式应为 YYYY-MM-DD HH:MM:SS，例如 2026-07-13 14:30:00")
                event_time_utc = local_to_utc_iso(event_time_raw)

            return _result(create_user_mood(content, score, mood_type_id, factors, event_time_utc))
        except ValueError as e:
            return _result(f"{ERROR}参数错误: {str(e)}")
        except Exception as e:
            return _result(f"{ERROR}创建失败: {str(e)}")