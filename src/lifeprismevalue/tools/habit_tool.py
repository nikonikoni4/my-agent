"""习惯打卡工具

移植自 lifeprism/llm/agent/tools/habit_tool.py。
"""

from __future__ import annotations

from typing import Any

from myagent.agent.core.tool.tool import Tool

from lifeprismevalue.data import habits as habit_data
from lifeprismevalue.tools.base import ERROR, SUCCESS, _result
from lifeprismevalue.utils.time_utils import get_local_today

_LEVEL_NAMES = {0: "萌芽", 1: "生根", 2: "成长", 3: "稳固", 4: "根深蒂固"}


def _format_frequency(freq: habit_data.FrequencyObject) -> str:
    """格式化频率对象为可读字符串。"""
    if freq.type == "custom" and freq.specific_days:
        day_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        days = "/".join(day_names[d - 1] for d in freq.specific_days if 1 <= d <= 7)
        return f"custom({days})"
    return freq.type


def _format_settlement(s: habit_data.SettlementItem) -> str:
    """格式化结算提示（挑战升级成功/失败预警）。"""
    result_text = "挑战成功" if s.result == "succeeded" else "挑战失败预警"
    lines = [
        f"[{result_text}] 习惯「{s.habit_name}」的挑战 {s.challenge_id}: "
        f"{s.from_level}级 -> {s.to_level}级, "
        f"完成 {s.completed_count}/{s.required_completions}"
    ]
    if s.result == "failed" and s.can_save_by_backfill:
        lines.append("  提示: 补签过去几天的打卡仍可挽救该挑战，建议询问用户是否补签")
    elif s.result == "failed":
        lines.append("  提示: 该挑战已无法挽救，请引导用户在前端处理结算（重新开始或暂停）")
    return "\n".join(lines)


def _format_habit(item: habit_data.HabitListItem) -> str:
    """格式化单个习惯（含当前挑战进度与今日打卡状态）。"""
    lines = [
        f"- {item.name} (id: {item.id})",
        f"  等级: {item.current_level}级({_LEVEL_NAMES.get(item.current_level, '?')}) | "
        f"频率: {_format_frequency(item.frequency)} | 状态: {item.status} | "
        f"Streak: {item.streak}天 | 今日已打卡: {'是' if item.today_completed else '否'}",
    ]
    if item.description:
        lines.append(f"  描述: {item.description}")
    if item.current_challenge:
        c = item.current_challenge
        lines.append(
            f"  当前挑战: {c.start_date} ~ {c.end_date}, "
            f"进度 {c.completed_count}/{c.required_completions}, "
            f"剩余可休息 {c.remaining_rest_days}天, 状态 {c.status}"
        )
    else:
        lines.append("  当前挑战: 无（habit_id 可能为空或未开始）")
    return "\n".join(lines)


class QueryUserHabitsTool(Tool):
    """查询用户习惯列表工具"""

    @property
    def name(self) -> str:
        return "query_user_habits"

    @property
    def description(self) -> str:
        return (
            "查询用户的习惯列表，包括习惯等级、频率、Streak连续天数、今日是否已打卡、"
            "当前挑战进度等。用户询问习惯情况、想打卡前不知道 habit_id 时，先调用此工具。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "status": {
                    "type": ["string", "null"],
                    "description": "可选，按状态过滤习惯",
                    "enum": ["active", "paused", None],
                },
            },
            "required": [],
        }

    async def execute(self, **kwargs: Any):
        try:
            status = kwargs.get("status")
            if status not in (None, "active", "paused"):
                return _result(f"{ERROR}参数错误: status 只能是 active/paused 或不传")
            response = habit_data.get_habit_service().get_habits(status)
            if not response.habits:
                return _result(f"{SUCCESS}当前没有{f'{status}状态的' if status else ''}习惯记录")
            header = f"共 {len(response.habits)} 个习惯:"
            return _result(f"{SUCCESS}{header}\n" + "\n".join(_format_habit(h) for h in response.habits))
        except Exception as e:
            return _result(f"{ERROR}查询习惯失败: {e}")


class CheckinHabitTool(Tool):
    """习惯今日打卡工具"""

    @property
    def name(self) -> str:
        return "checkin_habit"

    @property
    def description(self) -> str:
        return (
            "为指定习惯执行今日打卡。habit_id 需先通过 query_user_habits 获取。\n"
            "暂停状态的习惯、当日已打卡的习惯会返回错误。\n"
            "若打卡触发挑战升级或失败预警，返回结果中会包含结算提示。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "habit_id": {
                    "type": "string",
                    "description": "习惯 ID（格式 habit-xxxxxxxx，来自 query_user_habits）",
                },
            },
            "required": ["habit_id"],
        }

    async def execute(self, **kwargs: Any):
        try:
            habit_id = kwargs.get("habit_id", "")
            if not habit_id:
                return _result(f"{ERROR}请输入习惯ID（habit_id）")
            response = habit_data.get_habit_service().checkin_today(habit_id)
            parts = [
                f"{SUCCESS}打卡成功: {response.habit.name} (id: {response.checkin.habit_id}) "
                f"日期 {response.checkin.date}",
                _format_habit(response.habit),
            ]
            if response.settlement:
                parts.append(_format_settlement(response.settlement))
            return _result("\n".join(parts))
        except habit_data.ConflictError as e:
            return _result(f"{ERROR}打卡冲突: {e}")
        except habit_data.ValidationError as e:
            return _result(f"{ERROR}无法打卡: {e}")
        except habit_data.NotFoundError as e:
            return _result(f"{ERROR}习惯不存在: {e}")
        except Exception as e:
            return _result(f"{ERROR}打卡失败: {e}")


class CancelCheckinHabitTool(Tool):
    """取消习惯今日打卡工具"""

    @property
    def name(self) -> str:
        return "cancel_checkin_habit"

    @property
    def description(self) -> str:
        return "取消指定习惯的今日打卡（仅限今日，历史打卡不可取消）。取消后挑战进度会相应回退。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "habit_id": {
                    "type": "string",
                    "description": "习惯 ID（格式 habit-xxxxxxxx，来自 query_user_habits）",
                },
            },
            "required": ["habit_id"],
        }

    async def execute(self, **kwargs: Any):
        try:
            habit_id = kwargs.get("habit_id", "")
            if not habit_id:
                return _result(f"{ERROR}请输入习惯ID（habit_id）")
            today = get_local_today().isoformat()
            response = habit_data.get_habit_service().cancel_checkin(habit_id, today)
            parts = [
                f"{SUCCESS}已取消今日打卡: {response.habit.name} (id: {habit_id})",
                _format_habit(response.habit),
            ]
            if response.settlement:
                parts.append(_format_settlement(response.settlement))
            return _result("\n".join(parts))
        except habit_data.ValidationError as e:
            return _result(f"{ERROR}无法取消: {e}")
        except habit_data.NotFoundError as e:
            return _result(f"{ERROR}记录不存在: {e}")
        except Exception as e:
            return _result(f"{ERROR}取消打卡失败: {e}")


class BackfillCheckinTool(Tool):
    """习惯补签工具"""

    @property
    def name(self) -> str:
        return "backfill_checkin"

    @property
    def description(self) -> str:
        return (
            "为指定习惯补签历史打卡。规则：只能补签过去 6 天内（不含今日，今日请用 checkin_habit）、"
            "且在当前挑战周期内、当日尚未打卡的日期。\n"
            "补签可能修复断链并挽救即将失败的挑战。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "habit_id": {
                    "type": "string",
                    "description": "习惯 ID（格式 habit-xxxxxxxx，来自 query_user_habits）",
                },
                "dates": {
                    "type": "array",
                    "description": "补签日期列表，格式 YYYY-MM-DD，最多 6 项",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 6,
                },
            },
            "required": ["habit_id", "dates"],
        }

    async def execute(self, **kwargs: Any):
        try:
            habit_id = kwargs.get("habit_id", "")
            dates = kwargs.get("dates")
            if not habit_id:
                return _result(f"{ERROR}请输入习惯ID（habit_id）")
            if not dates or not isinstance(dates, list):
                return _result(f"{ERROR}请提供补签日期列表（dates）")

            service = habit_data.get_habit_service()
            detail = service.get_habit_detail(habit_id)
            if not detail.current_challenge:
                return _result(f"{ERROR}习惯 {habit_id} 当前无进行中的挑战，无法补签")

            req = habit_data.BackfillCheckInRequest(
                challenge_id=detail.current_challenge.id,
                items=[habit_data.BackfillCheckInItem(date=d) for d in dates],
            )
            response = service.backfill_checkin(habit_id, req)
            lines = [
                f"{SUCCESS}补签完成: {response.habit.name} (id: {habit_id}), "
                f"成功 {response.summary.succeeded}/{response.summary.total}"
            ]
            for r in response.results:
                if r.status == "succeeded":
                    lines.append(f"- {r.date}: 补签成功")
                    if r.settlement:
                        lines.append(_format_settlement(r.settlement))
                else:
                    lines.append(f"- {r.date}: 失败 ({r.message})")
            lines.append(_format_habit(response.habit))
            return _result("\n".join(lines))
        except habit_data.ValidationError as e:
            return _result(f"{ERROR}补签参数错误: {e}")
        except habit_data.NotFoundError as e:
            return _result(f"{ERROR}习惯不存在: {e}")
        except Exception as e:
            return _result(f"{ERROR}补签失败: {e}")